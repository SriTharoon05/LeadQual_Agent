"""Fact-level evidence contract shared by research, ICP, API, and exports."""
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FIELDS = {
    "employee_count": "Company size",
    "location": "Company location",
    "annual_revenue": "Annual revenue",
    "industry": "Industry",
    "seniority": "Current role / seniority",
    "employment": "Current employment",
}


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password):
            return value.strip()
    except ValueError:
        pass
    return None


def clean_sources(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(url for value in values if (url := safe_url(value))))[:30]


def citation_key(url):
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}]
    return urlunsplit(("", parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def normalize_evidence(raw, tool_sources):
    """Accept citations only when returned by the actual search tool.

    A source must have an accompanying supporting excerpt. This establishes
    attribution, not a guarantee of truth; conflicting evidence stays visible.
    """
    allowed = {url.rstrip("/"): url for url in clean_sources(tool_sources)}
    values = raw if isinstance(raw, dict) else {}
    result = {}
    for key, label in FIELDS.items():
        item = values.get(key)
        item = item if isinstance(item, dict) else {}
        sources = []
        for source in (item.get("sources") if isinstance(item.get("sources"), list) else [])[:5]:
            if not isinstance(source, dict):
                continue
            url = safe_url(source.get("url"))
            excerpt = source.get("excerpt")
            if url and url.rstrip("/") in allowed and isinstance(excerpt, str) and excerpt.strip():
                sources.append({"url": allowed[url.rstrip("/")],
                                "title": str(source.get("title") or urlsplit(url).hostname)[:160],
                                "excerpt": excerpt.strip()[:500]})
        value = item.get("value")
        value = str(value).strip()[:500] if isinstance(value, (str, int, float)) else ""
        status = item.get("status")
        if not isinstance(status, str) or status not in {"confirmed", "conflicting"} or not sources or not value:
            status, value, sources = "unclear", "Unclear — reliable evidence unavailable", []
        if key == "employment" and status == "confirmed" and value not in {"current", "former"}:
            status, value = "unclear", "Unclear — current employment not established"
        explanation = (str(item.get("explanation") or "")[:1000] if status != "unclear"
                       else "No reliable supporting evidence was available in this research.")
        result[key] = {"label": label, "value": value, "status": status,
                       "explanation": explanation,
                       "sources": sources}
    return result


def utc_checked_at():
    return datetime.now(timezone.utc).isoformat()


GROWTH_STRENGTH = {"hiring": 100, "expansion": 95, "technology_investment": 90,
                   "funding": 80, "acquisition": 70, "product_launch": 60, "leadership_change": 45}


def normalize_growth(raw, tool_sources, checked_at=None):
    """Only sourced, dated, relevant positive events earn ranking points."""
    raw = raw if isinstance(raw, dict) else {}
    allowed = {url.rstrip("/"): url for url in clean_sources(tool_sources)}
    try:
        reference = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        reference = datetime.now(timezone.utc).date()
    signals, seen = [], set()
    for item in (raw.get("signals") if isinstance(raw.get("signals"), list) else [])[:12]:
        if (not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                or item.get("kind") not in GROWTH_STRENGTH or item.get("relevant") is not True):
            continue
        try:
            occurred = date.fromisoformat(str(item.get("date")))
        except (TypeError, ValueError):
            continue
        age = (reference - occurred).days
        if not 0 <= age <= 730:
            continue
        sources = []
        for source in (item.get("sources", [])[:3] if isinstance(item.get("sources"), list) else []):
            if not isinstance(source, dict):
                continue
            url, excerpt = safe_url(source.get("url")), str(source.get("excerpt") or "").strip()
            if url and url.rstrip("/") in allowed and excerpt:
                sources.append({"url": allowed[url.rstrip("/")], "title": str(source.get("title") or urlsplit(url).hostname)[:160], "excerpt": excerpt[:500]})
        summary = str(item.get("summary") or "").strip()[:700]
        key = (item["kind"], occurred.isoformat(), summary.casefold())
        if not sources or not summary or key in seen:
            continue
        seen.add(key)
        factor = 1.0 if age <= 180 else 0.8 if age <= 365 else 0.5
        signals.append({"kind": item["kind"], "date": occurred.isoformat(), "summary": summary,
                        "relevant": True, "sources": sources, "strength": round(GROWTH_STRENGTH[item["kind"]] * factor, 1)})
    score = min(100.0, max((item["strength"] for item in signals), default=0) + min(10, max(0, len({item["kind"] for item in signals}) - 1) * 5))
    sources = list({source["url"]: source for signal in signals for source in signal["sources"]}.values())
    return {"signals": signals, "score": round(score, 1), "status": "confirmed" if signals else "unclear",
            "value": "; ".join(f"{item['date']}: {item['summary']}" for item in signals) or "No sourced, dated growth trigger established",
            "sources": sources, "explanation": "Ranking uses relevant events from the 24 months preceding this research: hiring, expansion and technology investment rank highest. Undated or unsupported summaries earn no points. These are demand indicators, not proof of buying intent."}


def proof_rows(web_data, icp_data, contact_data=None, icp_prompt=""):
    evidence = web_data.get("evidence")
    has_evidence = isinstance(evidence, dict)
    checks = icp_data.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    evidence = normalize_evidence(evidence, web_data.get("sources", []))
    rows = []
    for key in (FIELDS if has_evidence else ()):
        check = checks.get(key)
        check = check if isinstance(check, dict) else {}
        verdict = check.get("verdict")
        if not isinstance(verdict, str) or verdict not in {"match", "mismatch", "unclear", "not_specified"}:
            verdict = "unclear"
        rows.append({"field": key, **evidence[key],
                     "requirement": str(check.get("requirement") or "Not specified"),
                     "verdict": verdict,
                     "reasoning": str(check.get("reasoning") or ""),
                     "required": check.get("required", verdict != "not_specified"),
                     "checked_at": web_data.get("checked_at")})
    for key, check in checks.items():
        if key.startswith("additional_") and isinstance(check, dict):
            rows.append({"field": key, "label": key.removeprefix("additional_").replace("_", " ").title(),
                         "value": str(check.get("value") or "Unclear"), "status": "confirmed" if isinstance(check.get("verdict"), str) and check.get("verdict") in {"match", "mismatch"} and check.get("sources") else "unclear",
                         "verdict": check.get("verdict") if isinstance(check.get("verdict"), str) else "unclear", "requirement": str(check.get("requirement") or "Not specified"),
                         "required": check.get("required", True) is not False, "reasoning": str(check.get("reasoning") or ""),
                         "explanation": "Additional criterion from the supplied ICP", "sources": check.get("sources") if isinstance(check.get("sources"), list) else [], "checked_at": web_data.get("checked_at")})
    if contact_data is not None:
        from app.validators.contact_policy import contact_proof
        rows.append(contact_proof(contact_data, icp_prompt))
    growth = normalize_growth(web_data.get("growth_evidence"), web_data.get("sources", []), web_data.get("checked_at"))
    rows.append({"field": "growth", "label": "Growth signals", **growth,
                 "requirement": "Prioritize relevant hiring, expansion and business/technology investment",
                 "verdict": "match" if growth["signals"] else "unclear", "required": False,
                 "reasoning": f"Growth ranking score: {growth['score']}/100. No evidence means unknown demand, not proof of no growth.",
                 "checked_at": web_data.get("checked_at")})
    return rows
