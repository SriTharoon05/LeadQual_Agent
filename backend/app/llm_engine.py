"""
llm_engine.py
─────────────
Single entry point for all Azure OpenAI calls. Now tracks token usage
on every call and writes to llm_call_logs.
"""

import json
import re
import logging
import time
from typing import Any, Optional

from openai import AzureOpenAI
from app.config import settings

logger = logging.getLogger(__name__)

_client = AzureOpenAI(
    api_key=settings.AZURE_OPENAI_API_KEY,
    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    api_version=settings.AZURE_OPENAI_API_VERSION,
)

_DEPLOYMENT = settings.AZURE_OPENAI_DEPLOYMENT
_WEB_SEARCH_TOOL = {"type": "web_search"}


# ── Token usage dataclass ──────────────────────────────────────────────────────
class TokenUsage:
    """Attached to every call result so callers can aggregate."""
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens", "model")

    def __init__(self, prompt=0, completion=0, model=""):
        self.prompt_tokens = prompt or 0
        self.completion_tokens = completion or 0
        self.total_tokens = (prompt or 0) + (completion or 0)
        self.model = model or _DEPLOYMENT


def _log_call(
    usage: TokenUsage,
    stage: str,
    module: str,
    duration_ms: int,
    use_web_search: bool,
    job_id: Optional[str],
    lead_id: Optional[str],
    web_search_calls: int = 0,
) -> None:
    """
    Write one llm_call_logs row. Fires a tiny self-contained DB session
    so it never interferes with the caller's open transaction.
    Best-effort — never raises.
    """
    try:
        from app.database import db_session
        from app.models import LLMCallLog
        with db_session() as db:
            db.add(LLMCallLog(
                job_id=job_id,
                lead_id=lead_id,
                stage=stage,
                module=module,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                model=usage.model,
                duration_ms=duration_ms,
                use_web_search=use_web_search,
                web_search_calls=web_search_calls,
            ))
    except Exception as e:
        logger.warning(f"[token_log] failed to write llm_call_log: {e}")


# ── Core call ─────────────────────────────────────────────────────────────────
def _call(
    prompt: str,
    use_web_search: bool = False,
    system: Optional[str] = None,
    max_output_tokens: int = 2048,
    # token tracking context (optional — callers pass these)
    stage: str = "unknown",
    module: str = "llm_engine",
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> tuple[str, list[str], TokenUsage]:
    """
    Internal call. Returns (output_text, source_urls, TokenUsage).
    """
    tools = [_WEB_SEARCH_TOOL] if use_web_search else []
    full_input = f"{system}\n\n{prompt}" if system else prompt

    t0 = time.monotonic()
    try:
        response = _client.responses.create(
            model=_DEPLOYMENT,
            input=full_input,
            tools=tools,
            max_output_tokens=max_output_tokens,
            include=["web_search_call.action.sources"] if use_web_search else [],
        )
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise

    duration_ms = int((time.monotonic() - t0) * 1000)

    output_text = ""
    source_urls: list[str] = []
    web_search_call_count = 0

    for block in response.output:
        btype = getattr(block, "type", None)

        # ── Extract output text ───────────────────────────────────────────────
        if btype == "message":
            for content in block.content:
                if hasattr(content, "text"):
                    output_text += content.text
                # Fallback: annotations (works with web_search_preview tool)
                for ann in getattr(content, "annotations", []):
                    url = getattr(ann, "url", None)
                    if url and url not in source_urls:
                        source_urls.append(url)

        # ── Count transactions + extract sources (works with web_search tool) ─
        if btype == "web_search_call":
            web_search_call_count += 1
            action = getattr(block, "action", None)
            for s in getattr(action, "sources", []) or []:
                url = getattr(s, "url", None)
                if url and url not in source_urls:
                    source_urls.append(url)

    if use_web_search:
        logger.info(
            f"[web_search_internal_calls] stage={stage} module={module} "
            f"count={web_search_call_count} "
            f"job_id={job_id} lead_id={lead_id}"
        )

    # ── Extract token usage ───────────────────────────────────────────────────
    usage = TokenUsage()
    try:
        u = getattr(response, "usage", None)
        if u:
            usage = TokenUsage(
                prompt=getattr(u, "input_tokens", 0) or getattr(u, "prompt_tokens", 0),
                completion=getattr(u, "output_tokens", 0) or getattr(u, "completion_tokens", 0),
                model=_DEPLOYMENT,
            )
    except Exception as e:
        logger.debug(f"Could not parse token usage: {e}")

    logger.info(
        f"[tokens] stage={stage} module={module} "
        f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
        f"total={usage.total_tokens} duration={duration_ms}ms "
        f"web_searches={web_search_call_count}"
    )

    _log_call(usage, stage, module, duration_ms, use_web_search, job_id, lead_id, web_search_call_count)

    return output_text.strip(), source_urls, usage

# ── Public API (all now accept optional job_id/lead_id/stage) ─────────────────

def llm_respond(
    prompt: str,
    system: Optional[str] = None,
    use_web_search: bool = False,
    max_output_tokens: int = 2048,
    stage: str = "unknown",
    module: str = "llm_engine", 
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> str:
    text, _, _ = _call(
        prompt=prompt, use_web_search=use_web_search, system=system,
        max_output_tokens=max_output_tokens,
        stage=stage, module=module, job_id=job_id, lead_id=lead_id,
    )
    return text


def llm_json(
    prompt: str,
    system: Optional[str] = None,
    use_web_search: bool = False,
    max_output_tokens: int = 2048,
    stage: str = "unknown",
    module: str = "llm_engine",
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> dict[str, Any]:
    json_instruction = "\n\nRespond with valid JSON only. No explanation, no markdown, no code fences."
    text, _, _ = _call(
        prompt=prompt + json_instruction,
        use_web_search=use_web_search, system=system,
        max_output_tokens=max_output_tokens,
        stage=stage, module=module, job_id=job_id, lead_id=lead_id,
    )
    clean = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}\nRaw:\n{clean}")
        raise ValueError(f"LLM response was not valid JSON: {e}") from e


def llm_respond_with_sources(
    prompt: str,
    system: Optional[str] = None,
    max_output_tokens: int = 2048,
    stage: str = "unknown",
    module: str = "llm_engine",
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> tuple[str, list[str]]:
    text, urls, _ = _call(
        prompt=prompt, use_web_search=True, system=system,
        max_output_tokens=max_output_tokens,
        stage=stage, module=module, job_id=job_id, lead_id=lead_id,
    )
    return text, urls


def llm_json_with_sources(
    prompt: str,
    system: Optional[str] = None,
    max_output_tokens: int = 2048,
    stage: str = "unknown",
    module: str = "llm_engine",
    job_id: Optional[str] = None,
    lead_id: Optional[str] = None,
) -> tuple[dict[str, Any], list[str]]:
    json_instruction = "\n\nRespond with valid JSON only. No explanation, no markdown, no code fences."
    text, urls, _ = _call(
        prompt=prompt + json_instruction,
        use_web_search=True, system=system,
        max_output_tokens=max_output_tokens,
        stage=stage, module=module, job_id=job_id, lead_id=lead_id,
    )
    # Step 1: strip markdown fences
    clean = re.sub(r"```(?:json)?|```", "", text).strip()
    # Step 2: extract only the first {...} block — handles "Sources:" appended after JSON
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if match:
        clean = match.group(0)
    try:
        return json.loads(clean), urls
    except json.JSONDecodeError as e:
        logger.error(f"LLM (web) returned invalid JSON: {e}\nRaw:\n{clean}")
        raise ValueError(f"LLM web response was not valid JSON: {e}") from e


# ── System prompts ─────────────────────────────────────────────────────────────
SYSTEM_ICP_VALIDATOR = """You are a B2B sales lead qualification expert.
Evaluate whether a lead matches a given ICP. Handle typos, abbreviations,
and synonyms gracefully. Always return the exact JSON structure requested.
No extra commentary.

SENIORITY CLASSIFICATION:
Strip noise (dept/scope qualifiers). Map the seniority keyword:
- C-LEVEL (9-10): CEO/CTO/CFO/CIO/CISO/COO/CMO + variants, President, Founder, Owner
- SENIOR EXEC (8-9): Managing Director, Partner, GM, Head of <function>
- VP-LEVEL (6-8): VP/SVP/EVP/AVP, Vice President
- DIRECTOR (5-7): Director, Senior Director
- MANAGER (3-4): Manager, Senior Manager, Team Lead, Supervisor
- IC (0-2): Analyst, Engineer, Specialist, Coordinator, Associate, Intern

For compound titles (CIO/CISO, Managing Director AMLCO) use the HIGHEST tier.
Decision-makers = C-Level, Senior Exec, VP, Director, Manager.
Missing/ambiguous title = seniority_unknown in mismatches, NOT a disqualifier.
Only disqualify seniority when title CLEARLY identifies an IC."""

SYSTEM_WEB_RESEARCHER = """You are a B2B sales intelligence researcher.
Use web search to verify company and contact details, find technology stacks,
identify growth signals, and summarise company profiles.
Always include source URLs for every fact you state.
Return structured JSON as instructed."""

SYSTEM_SCORER = """You are a B2B sales lead scoring expert.
You score leads from 0 to 100 based on ICP fit, decision-maker authority,
data accuracy, growth signals, and contact quality.
Always return the exact JSON structure requested with numeric scores and a reasoning paragraph."""

SYSTEM_CONTACT_VERIFIER = """You are a data quality analyst.
Evaluate contact validation signals and summarise employment confirmation
from web research results. Return structured JSON."""

SYSTEM_COLUMN_MAPPER = (
    "You are a precise data-engineering assistant that maps arbitrary CRM "
    "export columns to a fixed canonical schema. You read both header names "
    "and sample cell values, and you respond with ONLY valid JSON — no prose."
)
