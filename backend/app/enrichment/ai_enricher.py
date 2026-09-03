"""
enrichment/ai_enricher.py
──────────────────────────
Fills canonical columns 19 (Company Description) and 20 (Technologies Used)
using the Stage 2 WebResearchResult.  (HARDENED, production-grade edition)

Thin post-processor: does NOT make new LLM calls unless a field is still empty
after web research. In that case it makes ONE targeted gap-fill call.

Design principle: MUST NOT crash the Celery task — None inputs, a None
web_result, or malformed LLM output all degrade to safe values.

Import:
    from app.enrichment.ai_enricher import enrich_lead
    enriched = enrich_lead(lead_data, web_result)
"""

import logging

from app.llm_engine import llm_json_with_sources, SYSTEM_WEB_RESEARCHER

logger = logging.getLogger(__name__)

MAX_DESC_CHARS  = 500
MAX_TECHS_CHARS = 300
NOT_FOUND = "Not found"


# ── Safe helpers ──────────────────────────────────────────────────────────────
def _s(value, default: str = "") -> str:
    """None-safe stripped string; joins lists (e.g. technologies list)."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ", ".join(_s(v) for v in value if v is not None).strip()
    try:
        return str(value).strip()
    except Exception:
        return default


def _clip(text: str, limit: int) -> str:
    text = _s(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ── Public API ────────────────────────────────────────────────────────────────
def enrich_lead(lead_data, web_result=None,
                job_id=None, lead_id=None) -> dict:
    """
    Return {"company_description": str, "technologies_used": str}.
    Both values are always non-empty strings. Never raises.
    """
    if not isinstance(lead_data, dict):
        lead_data = {}

    company = _s(lead_data.get("Company Name"))

    # Pull whatever Stage 2 found — None-safe via getattr + _s.
    desc  = _clip(_s(getattr(web_result, "company_description", "")), MAX_DESC_CHARS)
    techs = _clip(_s(getattr(web_result, "technologies_used", "")), MAX_TECHS_CHARS)

    if desc and techs:
        logger.debug(f"enrich_lead: both fields already filled for '{company}'")
        return {"company_description": desc, "technologies_used": techs}

    # Can't gap-fill without a company name — return what we have.
    if not company:
        return {
            "company_description": desc or NOT_FOUND,
            "technologies_used":   techs or NOT_FOUND,
        }

    logger.info(
        f"enrich_lead: gap fill for '{company}' "
        f"(desc={'ok' if desc else 'missing'}, techs={'ok' if techs else 'missing'})"
    )

    fallback = _gap_fill(lead_data, desc, techs, job_id=job_id, lead_id=lead_id)

    return {
        "company_description": _clip(fallback.get("company_description") or desc or NOT_FOUND, MAX_DESC_CHARS),
        "technologies_used":   _clip(fallback.get("technologies_used")   or techs or NOT_FOUND, MAX_TECHS_CHARS),
    }


# ── Fallback LLM call ─────────────────────────────────────────────────────────
def _gap_fill(lead_data, existing_desc, existing_techs,
              job_id=None, lead_id=None) -> dict:
    """One LLM+web call to fill missing col 19 / col 20. Never raises."""
    company  = _s(lead_data.get("Company Name"))
    website  = _s(lead_data.get("Company Website"))
    industry = _s(lead_data.get("Industry"))

    prompt = f"""
You are filling missing fields for a B2B sales lead record.

COMPANY: {company}
WEBSITE: {website or "unknown"}
INDUSTRY: {industry or "unknown"}

FIELDS ALREADY FILLED (do NOT change these):
  company_description: {f'"{existing_desc}"' if existing_desc else "MISSING — needs to be filled"}
  technologies_used:   {f'"{existing_techs}"' if existing_techs else "MISSING — needs to be filled"}

TASK: Use web search to fill the MISSING fields only. If you use the WEBSITE,
prioritise information from that exact domain to identify the correct company.

Definitions:
- company_description: 2-3 sentence plain-English summary of what this company
  does, who their customers are, and their core value proposition. Max 300 chars.
- technologies_used: comma-separated list of software/platforms/tools this
  company uses (CRM, ERP, cloud, marketing stack, etc.). Example:
  "Salesforce, AWS, Slack". Max 200 chars.

Return ONLY valid JSON with exactly these two keys:
{{
  "company_description": "<filled or repeat the existing value>",
  "technologies_used":   "<filled or repeat the existing value>"
}}
""".strip()

    try:
        raw = llm_json_with_sources(prompt, system=SYSTEM_WEB_RESEARCHER,
                            stage="enrich", module="ai_enricher",
                            job_id=job_id, lead_id=lead_id)

        # Defensive unpacking — engine returns (dict, sources) but never trust it.
        if isinstance(raw, tuple) and len(raw) >= 1:
            result = raw[0]
        else:
            result = raw
        if not isinstance(result, dict):
            result = {}

        return {
            "company_description": _s(result.get("company_description")) or existing_desc,
            "technologies_used":   _s(result.get("technologies_used"))   or existing_techs,
        }
    except Exception as e:
        logger.error(f"ai_enricher gap fill failed for '{company}': {e}")
        return {"company_description": existing_desc, "technologies_used": existing_techs}
