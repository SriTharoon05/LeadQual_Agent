"""
validators/web_researcher.py
─────────────────────────────
Stage 2 — Web Research & Verification.

Single combined LLM call per lead (replaces 5 separate calls).
Cuts token usage by ~65% vs the original multi-search approach.

Design principle: MUST NOT crash the Celery task. Every field is coerced
to a safe type; the external call is guarded; malformed output degrades
gracefully instead of raising.

Import:
    from app.validators.web_researcher import research_lead
    result: WebResearchResult = research_lead(lead_data)
"""

import json
import logging
from dataclasses import dataclass, field

from app.llm_engine import llm_json_with_sources, SYSTEM_WEB_RESEARCHER

logger = logging.getLogger(__name__)

# ── Limits (protect DB columns + token budget) ────────────────────────────────
MAX_DESCRIPTION_CHARS = 2000
MAX_SUMMARY_CHARS     = 1000
MAX_GROWTH_CHARS      = 2000
MAX_TECHS             = 40
MAX_TECH_LEN          = 80
MAX_SOURCES           = 25


# ── Safe-coercion helpers ─────────────────────────────────────────────────────
def _s(value, default: str = "") -> str:
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


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "confirmed")
    return bool(value)


def _as_tech_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = []
        for chunk in value.replace(";", ",").replace("\n", ",").split(","):
            t = chunk.strip()
            if t:
                parts.append(t)
        return parts
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            t = _s(item)
            if t:
                out.append(t)
        return out
    t = _s(value)
    return [t] if t else []


def _clip(text: str, limit: int) -> str:
    text = _s(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class WebResearchResult:
    company_verified: bool = False
    person_verified: bool = False
    industry_confirmed: bool = False
    employee_count_confirmed: bool = False
    technologies_found: list[str] = field(default_factory=list)
    growth_signals: str = ""
    company_summary: str = ""
    company_description: str = ""   # → col 19
    technologies_used: str = ""     # → col 20
    sources: list[str] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "company_verified":         self.company_verified,
            "person_verified":          self.person_verified,
            "industry_confirmed":       self.industry_confirmed,
            "employee_count_confirmed": self.employee_count_confirmed,
            "technologies_found":       self.technologies_found,
            "growth_signals":           self.growth_signals,
            "company_summary":          self.company_summary,
            "company_description":      self.company_description,
            "technologies_used":        self.technologies_used,
            "sources":                  self.sources,
        }


# ── Public API ────────────────────────────────────────────────────────────────
def research_lead(lead_data: dict,
                  job_id: str = None, lead_id: str = None) -> WebResearchResult:
    """
    Run Stage 2 web research using ONE combined LLM+web-search call.
    Never raises — returns safe defaults on any failure.
    """
    if not isinstance(lead_data, dict):
        logger.warning("research_lead: lead_data is not a dict — skipping")
        return _safe_default("Invalid lead data")

    company        = _s(lead_data.get("Company Name"))
    website        = _s(lead_data.get("Company Website"))
    person         = _s(lead_data.get("Contact Name"))
    title          = _s(lead_data.get("Job Title"))
    linkedin       = _s(lead_data.get("LinkedIn Profile URL"))
    industry       = _s(lead_data.get("Industry"))
    employee_count = _s(lead_data.get("Employee Count"))

    if not company:
        logger.warning(f"research_lead: no company name (person='{person}') — skipping")
        return _safe_default("No company name provided")

    website_line  = f"  Website:        {website}\n" if website  else ""
    linkedin_line = f"  LinkedIn:       {linkedin}\n" if linkedin else ""
    website_hint  = (
        "IMPORTANT: Use the Website above to identify the EXACT company and "
        "prioritise information from that domain. Disambiguate it from other "
        "companies with similar names.\n\n"
    ) if website else ""

    prompt = f"""
You are researching a B2B sales lead. Use web search to find ALL the information 
below in ONE SINGLE web search pass. You have a maximum of 1-2 searches total — 
use them wisely by combining all research goals into one query.
DO NOT make separate searches for company, person, tech stack, and growth signals.
Combine everything into one comprehensive search.

LEAD CONTEXT:
  Company:        {company}
{website_line}  Contact:        {person}
{linkedin_line}  Job Title:      {title}
  Industry:       {industry}
  Employee Count: {employee_count}

{website_hint}RESEARCH TASKS (cover all in one search session):
1. COMPANY OVERVIEW — verify employees, revenue range, industry. Confirm or correct the LEAD CONTEXT above.
2. PERSON VERIFY — confirm this person works at this company in this role. If a LinkedIn URL is provided treat it as strong evidence — visit/verify it. Set person_verified=true if LinkedIn OR another credible source confirms employment.
3. TECH STACK — find software, platforms, tools this company uses (job postings, G2/Capterra, BuiltWith, their site).
4. GROWTH SIGNALS — find recent, dated signals: funding, headcount growth, new offices, product launches, acquisitions, or executive hires. Prefer the last 24 months and state dates when available.
5. COMPANY DESCRIPTION — write a 2–3 sentence plain-English description of what this company does, who their customers are, and their value proposition.

Return ONLY this exact JSON (all keys required, use null for unknown fields):
{{
  "company_verified":         <true|false>,
  "person_verified":          <true|false>,
  "industry_confirmed":       <true|false>,
  "employee_count_confirmed": <true|false>,
  "technologies_found":       ["<tech1>", "<tech2>"],
  "technologies_used": "<comma-separated string of tools>",
  "growth_signals":           "<paragraph summary of growth signals, or 'None found'>",
  "company_description":      "<2-3 sentence description>",
  "company_summary":          "<one sentence summary>"
}}
""".strip()

    all_sources: list[str] = []
    partial: dict = {}

    try:
        result = llm_json_with_sources(
            prompt,
            system=SYSTEM_WEB_RESEARCHER,
            max_output_tokens=800,
            stage="web",
            module="web_researcher.combined",
            job_id=job_id,
            lead_id=lead_id,
        )

        if isinstance(result, tuple) and len(result) == 2:
            partial, urls = result
        else:
            partial, urls = (result if isinstance(result, dict) else {}), []

        if not isinstance(partial, dict):
            partial = {}
        if not isinstance(urls, (list, tuple)):
            urls = []

        all_sources = list(urls)

    except Exception as e:
        logger.warning(f"Web research combined call failed for '{company}': {e}")

    if not partial:
        d = _safe_default("Combined web search failed")
        d.sources = all_sources[:MAX_SOURCES]
        return d

    merged = _merge([partial], all_sources)
    try:
        merged.raw_output = json.dumps(merged.to_dict(), default=str)
    except Exception:
        merged.raw_output = "{}"

    logger.debug(
        f"Web research [{company}] verified={merged.company_verified} "
        f"person={merged.person_verified} techs={len(merged.technologies_found)} "
        f"sources={len(merged.sources)}"
    )
    return merged


# ── Merger (fully type-safe) ──────────────────────────────────────────────────
def _merge(partials: list[dict], sources: list[str]) -> WebResearchResult:
    result = WebResearchResult()
    seen: set[str] = set()
    clean_sources: list[str] = []
    for s in (sources or []):
        s = _s(s)
        if s and s not in seen:
            seen.add(s)
            clean_sources.append(s)
    result.sources = clean_sources[:MAX_SOURCES]

    for p in partials:
        if not isinstance(p, dict):
            continue

        result.company_verified         |= _as_bool(p.get("company_verified"))
        result.person_verified          |= _as_bool(p.get("person_verified"))
        result.industry_confirmed       |= _as_bool(p.get("industry_confirmed"))
        result.employee_count_confirmed |= _as_bool(p.get("employee_count_confirmed"))

        for tech in _as_tech_list(p.get("technologies_found")):
            t = _clip(tech, MAX_TECH_LEN)
            if t and t.lower() not in {x.lower() for x in result.technologies_found}:
                if len(result.technologies_found) < MAX_TECHS:
                    result.technologies_found.append(t)

        if not result.growth_signals:
            g = _s(p.get("growth_signals"))
            if g and g.lower() not in ("none", "none found", "n/a", "null"):
                result.growth_signals = _clip(g, MAX_GROWTH_CHARS)

        if not result.company_summary:
            s = _s(p.get("company_summary"))
            if s:
                result.company_summary = _clip(s, MAX_SUMMARY_CHARS)

        if not result.company_description:
            d = _s(p.get("company_description"))
            if d:
                result.company_description = _clip(d, MAX_DESCRIPTION_CHARS)

    result.technologies_used = ", ".join(result.technologies_found)

    if not result.company_description and result.company_summary:
        result.company_description = result.company_summary

    return result


# ── Safe helpers ──────────────────────────────────────────────────────────────
def _safe_default(reason: str) -> WebResearchResult:
    r = WebResearchResult()
    r.growth_signals = f"Web research skipped: {reason}"
    try:
        r.raw_output = json.dumps({"error": reason})
    except Exception:
        r.raw_output = "{}"
    return r
