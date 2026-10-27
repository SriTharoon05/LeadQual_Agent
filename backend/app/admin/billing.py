"""Meter provider-reported usage; never infer Bing requests from tool items."""
import json
import random
import time
from decimal import Decimal
from uuid import uuid4

from app.admin.concurrency import before_model_call, CapacityDeferred
from app.admin.settings import load_settings, redis_client, utcnow
from app.admin.telemetry import _context, current_context, enqueue, publish_event, valid_id, mark_rate_limit
from app.utils.job_pause import assert_job_not_paused


def get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def amount(tokens, rate, divisor=1000000):
    return Decimal(tokens) * Decimal(str(rate)) / Decimal(divisor)


def model_matches(actual, expected):
    actual, expected = str(actual or "").casefold(), str(expected or "").casefold()
    return bool(expected and (actual == expected or actual.startswith(expected + "-")))


def rate_limit_delay(exc, attempt=0):
    if get(exc, "status_code") != 429:
        return None
    headers = get(get(exc, "response"), "headers", {}) or {}
    for key, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        try:
            return max(1, min(3600, float(headers[key]) / divisor))
        except (ValueError, TypeError, KeyError):
            pass
    return random.uniform(60, min(300, 90 * (2 ** min(max(int(attempt), 0), 2))))


class metered_call:
    def __init__(self, *, stage, module, request_model, kind="llm", job_id=None, lead_id=None, web=False):
        self.stage, self.module, self.request_model = stage, module, request_model
        self.kind, self.web = kind, web
        self.job_id, self.lead_id = job_id, lead_id
        self.response = None

    def __enter__(self):
        context = current_context()
        before_model_call()
        try:
            self.settings = load_settings()
        except Exception:
            self.settings = None
        # Covers LLM/search requests and every embedding batch, not just new
        # qualification tasks. Check after potentially slow settings/lease reads
        # and before journaling an attempt or admitting a provider request.
        assert_job_not_paused(self.job_id or context.get("job_id"),
                              lead_id=self.lead_id or context.get("lead_id"))
        self.context_token = _context.set({**context, "stage": self.stage, "component": self.module,
            **({"job_id": str(self.job_id)} if self.job_id else {}),
            **({"lead_id": str(self.lead_id)} if self.lead_id else {})})
        self.id = str(uuid4())
        self.created_at = utcnow().isoformat()
        current = current_context()
        try:
            enqueue("usage", {"id": self.id, "job_id": valid_id(current.get("job_id")),
                "lead_id": valid_id(current.get("lead_id")), "stage": self.stage[:50], "module": self.module[:100],
                "model": self.request_model[:100], "status": "started", "cost_complete": False,
                "use_web_search": self.web, "created_at": self.created_at,
                "pricing_snapshot_json": json.dumps({"version": self.settings.get("version") if self.settings else None,
                    "pricing": self.settings.get("pricing") if self.settings else None, "kind": self.kind, "estimated": True})},
                require_durable=True)
        except Exception:
            _context.reset(self.context_token)
            raise CapacityDeferred("Usage journal unavailable; no paid request was started") from None
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, _traceback):
        try:
            self._record(exc)
        except Exception:
            # Do not change a paid API response because telemetry is unavailable.
            publish_event("Usage accounting could not be recorded; cost reports may be incomplete.", severity="ERROR", kind="usage_gap")
        finally:
            _context.reset(self.context_token)
        return False

    def _record(self, exc):
        response, context = self.response, current_context()
        usage = get(response, "usage")
        prompt = count(get(usage, "input_tokens", get(usage, "prompt_tokens")))
        completion = count(get(usage, "output_tokens", get(usage, "completion_tokens")))
        if self.kind == "embedding" and usage is not None:
            completion = 0
        details = get(usage, "input_tokens_details", get(usage, "prompt_tokens_details"))
        cached = 0 if self.kind == "embedding" else count(get(details, "cached_tokens"))
        if cached is not None and prompt is not None and cached > prompt:
            cached = None
        raw = response.model_dump() if response is not None and hasattr(response, "model_dump") else response
        search_requests = count(get(get(get(raw, "tool_usage"), "web_search"), "num_requests")) if self.web else 0
        tool_calls = sum(get(block, "type") == "web_search_call" for block in (get(response, "output", []) or []))
        actual_model = str(get(response, "model", "") or "")[:100] or None
        pricing = self.settings.get("pricing") if self.settings else None
        costs = {"cost_input_usd": None, "cost_cached_usd": None,
                 "cost_output_usd": None, "cost_search_usd": None}
        if pricing and prompt is not None and completion is not None:
            if self.kind == "embedding" and model_matches(actual_model, pricing["embedding_model"]):
                costs.update(cost_input_usd=amount(prompt, pricing["embedding_per_million"]),
                    cost_cached_usd=Decimal(0), cost_output_usd=Decimal(0))
            elif self.kind != "embedding" and model_matches(actual_model, pricing["model"]):
                cache = min(prompt, cached or 0)
                costs.update(cost_input_usd=amount(prompt - cache, pricing["input_per_million"]),
                    cost_cached_usd=amount(cache, pricing["cached_per_million"]),
                    cost_output_usd=amount(completion, pricing["output_per_million"]))
        if not self.web:
            costs["cost_search_usd"] = Decimal(0)
        elif pricing and search_requests is not None:
            costs["cost_search_usd"] = amount(search_requests, pricing["search_per_thousand"], 1000)
        known = [value for value in costs.values() if value is not None]
        complete = all(value is not None for value in costs.values()) and cached is not None and exc is None
        code = str(get(exc, "status_code", "") or "")[:80] or None
        has_usage = prompt is not None and completion is not None
        provider_state = get(response, "status")
        status = ("rate_limited" if code == "429" else "error" if exc or provider_state in {"failed", "cancelled"}
                  else "incomplete" if provider_state == "incomplete" else "success" if has_usage else "usage_missing")
        data = {"id": self.id, "job_id": valid_id(context.get("job_id")),
            "lead_id": valid_id(context.get("lead_id")), "stage": self.stage[:50], "module": self.module[:100],
            "model": self.request_model[:100], "billing_model": actual_model,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": count(get(usage, "total_tokens")) if usage is not None else None,
            "cached_prompt_tokens": cached, "web_search_calls": tool_calls,
            "search_requests": search_requests, "use_web_search": self.web,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "status": status, "error_code": code,
            "provider_request_id": str(get(response, "id", "") or get(exc, "request_id", ""))[:200] or None,
            **{name: str(value) if value is not None else None for name, value in costs.items()},
            "cost_total_usd": str(sum(known, Decimal(0))) if known else None,
            "cost_complete": complete, "created_at": self.created_at,
            "pricing_snapshot_json": json.dumps({"version": self.settings.get("version") if self.settings else None,
                "pricing": pricing, "kind": self.kind, "estimated": True,
                "search_meter": "tool_usage.web_search.num_requests", "cache_usage_reported": cached is not None})}
        if data["total_tokens"] is None and prompt is not None and completion is not None:
            data["total_tokens"] = prompt + completion
        enqueue("usage", data)
        if exc:
            retry = None
            headers = get(get(exc, "response"), "headers", {}) or {}
            try:
                retry = max(0, min(3600, int(float(headers.get("retry-after", "")))))
            except (ValueError, TypeError):
                pass
            if code == "429":
                retry = int(rate_limit_delay(exc) or 60) + 1
                mark_rate_limit()
                if self.kind != "embedding":
                    try:
                        redis_client().eval("""
                            local ttl=redis.call('TTL',KEYS[1])
                            if ttl < tonumber(ARGV[1]) then redis.call('SET',KEYS[1],'1','EX',ARGV[1]) end
                            return 1
                        """, 1, "ql:runtime:provider-cooldown", retry)
                    except Exception:
                        pass
            publish_event("Azure AI rate limit reached. Queued lead processing will retry; retry interactive chat if needed. Lower concurrency if it repeats."
                if code == "429" else f"AI request failed ({type(exc).__name__}{', HTTP ' + code if code else ''}). Saved stages are retained for retry.",
                severity="WARNING" if code == "429" else "ERROR", kind="rate_limit" if code == "429" else "provider_error",
                retry_after=retry)
        elif not has_usage:
            publish_event("The provider did not return token usage. This request is marked incomplete in the cost report.", kind="usage_gap")
        elif provider_state in {"failed", "cancelled", "incomplete"}:
            publish_event(f"The provider returned an {provider_state} response. Reported usage is retained in the cost breakdown.",
                          severity="ERROR" if provider_state == "failed" else "WARNING", kind="provider_error")
