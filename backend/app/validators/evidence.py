"""Fact-level evidence contract shared by research, ICP, API, and exports."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

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
        if status not in {"confirmed", "conflicting"} or not sources or not value:
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


def proof_rows(web_data, icp_data):
    evidence = web_data.get("evidence")
    if not isinstance(evidence, dict):
        return []  # Earlier jobs have no claim-level proofs; never fabricate them.
    checks = icp_data.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    evidence = normalize_evidence(evidence, web_data.get("sources", []))
    rows = []
    for key in FIELDS:
        check = checks.get(key)
        check = check if isinstance(check, dict) else {}
        verdict = check.get("verdict")
        if verdict not in {"match", "mismatch", "unclear", "not_specified"}:
            verdict = "unclear"
        rows.append({"field": key, **evidence[key],
                     "requirement": str(check.get("requirement") or "Not specified"),
                     "verdict": verdict,
                     "reasoning": str(check.get("reasoning") or ""),
                     "checked_at": web_data.get("checked_at")})
    return rows
