"""
scoring/scoring_engine.py
──────────────────────────
Combines Stage 1 + 2 + 3 results into a single 0–100 score. (HARDENED edition)

The LLM is the scoring judge; if it fails OR returns malformed output, a
DETERMINISTIC fallback computes the score arithmetically so the pipeline ALWAYS
produces a valid result.

ICP GATE: a lead with any MAJOR DISQUALIFIER from Stage 1 (e.g.
outside_target_geography, non_target_industry, missing_valid_contact_phone)
is always forced to "Low" priority — even if its weighted score is over 50.

Design principle: neither the LLM path NOR the fallback path may crash — every
field is coerced, every stage result is accessed defensively (they may be None).

Import:
    from app.scoring.scoring_engine import score_lead
    result = score_lead(lead_data, icp_result, web_result, contact_result)
"""

import json
import logging
import re
from dataclasses import dataclass

from app.config import SCORING_WEIGHTS, get_priority
from app.llm_engine import llm_json, SYSTEM_SCORER

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 1500
MAX_GROWTH_CHARS = 1000
MAX_OUTREACH_CHARS = 3000

# Fallback weights if config is somehow malformed.
_DEFAULT_WEIGHTS = {
    "icp_fit": 0.30, "decision_maker": 0.25, "data_accuracy": 0.25,
    "growth_signals": 0.15, "contact_quality": 0.05,
}


# ── Safe helpers ──────────────────────────────────────────────────────────────
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


def _num(value, default: float = 0.0) -> float:
    """Coerce to float, never raising. Handles None, '85', '85%', '85/100'."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return default
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            try:
                return float(m.group())
            except Exception:
                return default
    return default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _get(obj, attr, default=None):
    """Defensive getattr — stage results may be None or unexpected types."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _weights() -> dict:
    w = SCORING_WEIGHTS if isinstance(SCORING_WEIGHTS, dict) else {}
    # Merge over defaults so a missing key can't KeyError later.
    return {**_DEFAULT_WEIGHTS, **{k: _num(v, _DEFAULT_WEIGHTS.get(k, 0))
                                   for k, v in w.items()}}


def _priority(total: float) -> str:
    try:
        p = get_priority(total)
        return _s(p) or "Low"
    except Exception:
        # Local fallback tiers if config's get_priority misbehaves.
        if total >= 80:
            return "High"
        if total >= 50:
            return "Medium"
        return "Low"


def _priority_with_icp_gate(total: float, icp_result) -> str:
    """Mandatory ICP failures always produce Low priority."""
    major_disqualifiers = _get(
        icp_result, "major_disqualifiers", []
    ) or []

    if major_disqualifiers:
        return "Low"

    return _priority(total)


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class ScoreResult:
    total_score:     float = 0.0
    icp_score:       float = 0.0
    authority_score: float = 0.0
    data_score:      float = 0.0
    growth_score:    float = 0.0
    contact_score:   float = 0.0
    priority:        str   = "Low"
    reasoning:       str   = ""
    growth_signals:  str   = ""
    outreach_intelligence: str = ""
    raw_output:      str   = ""

    def to_dict(self) -> dict:
        return {
            "total_score":     self.total_score,
            "icp_score":       self.icp_score,
            "authority_score": self.authority_score,
            "data_score":      self.data_score,
            "growth_score":    self.growth_score,
            "contact_score":   self.contact_score,
            "priority":        self.priority,
            "reasoning":       self.reasoning,
            "growth_signals":  self.growth_signals,
            "outreach_intelligence": self.outreach_intelligence,
        }


# ── Public API ────────────────────────────────────────────────────────────────
def score_lead(lead_data, icp_result, web_result, contact_result,
               job_id=None, lead_id=None) -> ScoreResult:
    """
    Final scoring pass. Never raises. LLM path first; deterministic fallback on
    any failure or malformed output.
    """
    if not isinstance(lead_data, dict):
        lead_data = {}

    try:
        prompt = _build_prompt(lead_data, icp_result, web_result, contact_result)
        raw = llm_json(prompt, system=SYSTEM_SCORER, max_output_tokens=900,
                   stage="score", module="scoring_engine",
                   job_id=job_id, lead_id=lead_id)
        if not isinstance(raw, dict):
            logger.warning(f"scoring: LLM returned non-dict ({type(raw)}) — fallback")
            return _deterministic_fallback(icp_result, web_result, contact_result,
                                           "LLM non-dict output")
        result = _parse_response(raw, icp_result)
        try:
            result.raw_output = json.dumps(raw, default=str)
        except Exception:
            result.raw_output = "{}"
        logger.debug(
            f"Score [{_s(lead_data.get('Contact Name')) or '?'}] "
            f"total={result.total_score} priority={result.priority}"
        )
        return result
    except Exception as e:
        logger.error(
            f"LLM scoring failed for '{_s(lead_data.get('Contact Name')) or '?'}': "
            f"{e} — using fallback"
        )
        return _deterministic_fallback(icp_result, web_result, contact_result, str(e))


# ── Prompt ────────────────────────────────────────────────────────────────────
def _build_prompt(lead_data, icp_result, web_result, contact_result) -> str:
    w = _weights()

    # Only include non-empty lead fields, capped
    lead_summary = "\n".join(
        f"  {k}: {_s(v)[:80]}"          # ← cap each field at 80 chars
        for k, v in lead_data.items()
        if _s(v) and k in (             # ← only fields scorer actually needs
            "Contact Name", "Job Title", "Company Name",
            "Industry", "Employee Count", "Annual Revenue",
            "Email Address", "Phone Number",
        )
    ) or "  (no data)"

    return f"""
Score this B2B lead 0-100 using the stage results below.
Verified facts and ICP checks override uploaded claims. Unknown/conflicting facts
must be described as unclear, never asserted as verified. No employment evidence
means unclear, not former. Explain concrete values and mismatches in reasoning.

LEAD:
{lead_summary}

STAGE 1 — ICP:
  icp_match={_get(icp_result,'icp_match')} score={_num(_get(icp_result,'icp_score'))}/10
  matched={_get(icp_result,'matched_criteria',[])}
  mismatches={_get(icp_result,'mismatches',[])}
  disqualifiers={_get(icp_result,'major_disqualifiers',[])}
  dm_score={_num(_get(icp_result,'decision_maker_score'))}/10

STAGE 2 — WEB:
  company_ok={_get(web_result,'company_verified')}
  person_ok={_get(web_result,'person_verified')}
  industry_ok={_get(web_result,'industry_confirmed')}
  headcount_ok={_get(web_result,'employee_count_confirmed')}
  techs={len(_get(web_result,'technologies_found') or [])} found
  has_growth={bool(_get(web_result,'growth_signals') and 'none' not in str(_get(web_result,'growth_signals','')).lower())}
  company_summary={_s(_get(web_result,'company_summary'))[:400]}
  verified_facts={json.dumps(_get(web_result, "evidence", {}), default=str)}
  icp_checks={json.dumps(_get(icp_result, "checks", {}), default=str)}
  technologies={_s(_get(web_result,'technologies_found'))[:300]}
  growth_evidence={_s(_get(web_result,'growth_signals'))[:500]}

STAGE 3 — CONTACT:
  email_valid={_get(contact_result,'email_valid')}
  phone_valid={_get(contact_result,'phone_valid')}
  employed={_get(contact_result,'employment_confirmed')}
  quality={_num(_get(contact_result,'contact_quality_score'))}/10

WEIGHTS: icp={w['icp_fit']*100:.0f}% dm={w['decision_maker']*100:.0f}% data={w['data_accuracy']*100:.0f}% growth={w['growth_signals']*100:.0f}% contact={w['contact_quality']*100:.0f}%

Return ONLY this JSON:
{{
  "total_score": <float 0-100>,
  "icp_score": <float 0-100>,
  "authority_score": <float 0-100>,
  "data_score": <float 0-100>,
  "growth_score": <float 0-100>,
  "contact_score": <float 0-100>,
  "reasoning": "<2-3 sentence sales summary>",
  "growth_signals": "<1-2 sentence growth summary or 'None identified'>",
  "outreach_intelligence": {{
    "recommended_channel": "<email|phone|linkedin>",
    "best_angle": "<specific value proposition grounded in the evidence>",
    "opening_message": "<personalized, concise first-touch message; do not invent facts>",
    "talking_points": ["<point 1>", "<point 2>"],
    "objection_watchout": "<likely objection and how to handle it>"
  }}
}}""".strip()

# ── Response parser (type-safe) ───────────────────────────────────────────────
def _parse_response(raw: dict, icp_result=None) -> ScoreResult:
    # Accept the model's component scores, but compute the final score ourselves.
    # This prevents an LLM arithmetic error from being stored or displayed.
    icp_score       = _clamp(_num(raw.get("icp_score")))
    authority_score = _clamp(_num(raw.get("authority_score")))
    data_score      = _clamp(_num(raw.get("data_score")))
    growth_score    = _clamp(_num(raw.get("growth_score")))
    contact_score   = _clamp(_num(raw.get("contact_score")))

    w = _weights()
    total = round(
        icp_score * w["icp_fit"]
        + authority_score * w["decision_maker"]
        + data_score * w["data_accuracy"]
        + growth_score * w["growth_signals"]
        + contact_score * w["contact_quality"],
        1,
    )

    outreach = raw.get("outreach_intelligence")
    if isinstance(outreach, dict):
        outreach = json.dumps(outreach, ensure_ascii=False)

    return ScoreResult(
        total_score=total,
        icp_score=icp_score,
        authority_score=authority_score,
        data_score=data_score,
        growth_score=growth_score,
        contact_score=contact_score,
        priority=_priority_with_icp_gate(total, icp_result),
        reasoning=_s(raw.get("reasoning"))[:MAX_REASON_CHARS],
        growth_signals=_s(raw.get("growth_signals"))[:MAX_GROWTH_CHARS]
        or "None identified",
        outreach_intelligence=_s(outreach)[:MAX_OUTREACH_CHARS],
    )


# ── Deterministic fallback (no LLM) — also fully guarded ─────────────────────
def _deterministic_fallback(icp_result, web_result, contact_result, error_msg) -> ScoreResult:
    w = _weights()

    icp_s       = _clamp(_num(_get(icp_result, "icp_score")) * 10.0)
    authority_s = _clamp(_num(_get(icp_result, "decision_maker_score")) * 10.0)

    web_checks = sum(1 for f in (
        _get(web_result, "company_verified"),
        _get(web_result, "person_verified"),
        _get(web_result, "industry_confirmed"),
        _get(web_result, "employee_count_confirmed"),
    ) if bool(f))
    data_s = (web_checks / 4.0) * 100.0

    growth_raw = _s(_get(web_result, "growth_signals"))
    growth_s = 60.0 if growth_raw and "none" not in growth_raw.lower() else 10.0

    contact_s = _clamp(_num(_get(contact_result, "contact_quality_score")) * 10.0)

    total = _clamp(
        icp_s       * w["icp_fit"] +
        authority_s * w["decision_maker"] +
        data_s      * w["data_accuracy"] +
        growth_s    * w["growth_signals"] +
        contact_s   * w["contact_quality"]
    )
    total = round(total, 1)

    r = ScoreResult(
        total_score=     total,
        icp_score=       round(icp_s, 1),
        authority_score= round(authority_s, 1),
        data_score=      round(data_s, 1),
        growth_score=    round(growth_s, 1),
        contact_score=   round(contact_s, 1),
        priority=        _priority_with_icp_gate(total, icp_result),
        reasoning=       f"Fallback scoring used (LLM error: {_s(error_msg)[:200]})",
        growth_signals=  growth_raw or "None identified",
        outreach_intelligence=json.dumps({
            "recommended_channel": "email" if bool(_get(contact_result, "email_valid")) else "linkedin",
            "best_angle": "Lead with the verified ICP fit and a relevant business outcome.",
            "opening_message": "Introduce the relevant outcome, reference only verified company context, and ask for a short discovery call.",
            "talking_points": ["ICP-aligned business outcome", "Verified company context"],
            "objection_watchout": "Confirm current priorities before proposing a detailed solution.",
        }),
    )
    try:
        r.raw_output = json.dumps({"error": _s(error_msg), "fallback": True})
    except Exception:
        r.raw_output = "{}"
    return r
