"""
validators/icp_validator.py
───────────────────────────
Stage 1 — ICP Match & Decision-Maker Scoring.  (HARDENED, production-grade)

Single LLM call that:
  1. Semantically matches the lead against the user's ICP prompt
  2. Scores decision-maker authority from job title / seniority
  3. Identifies MAJOR DISQUALIFIERS — mandatory ICP failures that force the
     lead to be a non-match regardless of any positive signals.
  4. Returns structured JSON

Design principle: MUST NOT crash the Celery task, whatever the input or LLM
output. Every field is coerced to a safe type; malformed output degrades to a
safe default instead of raising.

Import:
    from app.validators.icp_validator import validate_icp
    result: ICPResult = validate_icp(lead_data, icp_prompt)
"""

import json
import logging
from dataclasses import dataclass, field

from app.llm_engine import llm_json, SYSTEM_ICP_VALIDATOR

logger = logging.getLogger(__name__)

ICP_MATCH_THRESHOLD = 6.0     # icp_match derived from score when LLM is unclear
MAX_REASON_CHARS    = 1000
MAX_LIST_ITEMS      = 30
MAX_ITEM_CHARS      = 200


# ── Seniority / decision-maker classification FRAMEWORK ───────────────────────
# A production system must REASON about ANY job title, not match a fixed list.
# This teaches the model HOW to determine a title's seniority tier and whether
# the person is a decision-maker — so it generalises to messy, compound, and
# previously-unseen titles ("Head of Enterprise Data Management & Analytics -
# Global Digital & Cybersecurity", "Managing Director, AMLCO", etc.).
# _SENIORITY_FRAMEWORK = """
# Classify EVERY job title into ONE seniority tier by REASONING about it — never
# rely on exact string matching. Apply this method to any title, even unseen ones:

# STEP A — Strip noise. Ignore department / function / scope qualifiers that do
# NOT change seniority (e.g. "of Information Security", "EMEA Operations",
# ", Global Digital & Cybersecurity", "AMLCO", "Business Process, Systems and
# Controls", "Advisory Services"). Focus on the SENIORITY KEYWORD.

# STEP B — Map the seniority keyword(s) to a tier:

#   • C-LEVEL  (top decision-maker)
#     - "Chief" + "Officer" in any form → Chief Information Officer, Chief
#       Innovation Officer, Chief Data Officer, Chief AI Officer, etc.
#     - Any C-x-O abbreviation: CEO, CFO, CTO, CIO, CISO, COO, CMO, CRO, CPO,
#       CDO, CHRO — INCLUDING slashed/combined forms (CIO / CISO, CIO/CTO).
#     - President, Founder, Co-Founder, Owner, Proprietor.

#   • SENIOR EXECUTIVE  (top decision-maker; org / business-unit leader)
#     - Managing Director, Partner, Executive Director, General Manager (GM),
#       "Head of <function>" (e.g. Head of IT, Head of IT Procurement, Head of
#       Enterprise Data Management). "Managing Director, <x>" and
#       "Partner, <x>" remain Senior Executive regardless of the trailing text.

#   • VP-LEVEL  (senior decision-maker)
#     - VP, SVP, EVP, AVP, Vice President, "Vice President of <function>".

#   • DIRECTOR  (decision-maker / strong influencer)
#     - Director, Senior Director, "Director of <function>",
#       "Director, <region/scope>".

#   • MANAGER  (mid-level; influencer, sometimes a decision-maker)
#     - Manager, Senior Manager, Group Manager, Team Lead, Supervisor.

#   • INDIVIDUAL CONTRIBUTOR  (usually NOT a decision-maker)
#     - Analyst, Engineer, Developer, Specialist, Coordinator, Associate,
#       Consultant, Administrator, Assistant, Intern.
#     - EXCEPTION: a leadership qualifier ("Lead", "Principal" heading a function,
#       "Staff" leading others) lifts it to Manager-level influence.

# STEP C — Compound / multiple titles. If a title lists several roles or is joined
# by "/", ",", "&", or "and" (e.g. "CIO / CISO", "Managing Director, AMLCO",
# "Partner, Business Process, Systems and Controls"), classify by the HIGHEST tier
# present.

# STEP D — Decision-making authority. C-Level, Senior Executive, VP, and Director
# ARE decision-makers. Managers are partial decision-makers / strong influencers.
# Individual contributors are generally not decision-makers.

# MATCHING RULE — After classifying the tier, compare it to the ICP's target
# seniority. If the classified tier is one the ICP asks for, it IS a seniority
# match. NEVER mark a genuine C-Level / Senior-Executive / VP / Director / Manager
# title as a seniority mismatch when the ICP targets that level. Only flag a
# seniority mismatch when the person is clearly BELOW every level the ICP
# targets (e.g. an individual contributor when the ICP wants Manager and above).
# """

# Decision-maker AUTHORITY SCORE guidance (0–10) — reuses the same tiers.
# _DM_TIER_HINT = """
# Score decision_maker_score from the seniority tier you determined:
#   9–10  C-Level (CEO/CTO/CFO/CIO/CISO/... , President, Founder, Owner)
#   8–9   Senior Executive (Managing Director, Partner, GM, Head of <function>)
#   6–8   VP-Level (VP/SVP/EVP/AVP)
#   5–7   Director (Director / Senior Director)
#   3–4   Manager (Manager / Senior Manager / Team Lead)
#   0–2   Individual Contributor (Analyst, Engineer, Specialist, Associate, Intern)
# Use the HIGHEST tier for compound titles.
# """


# ── Safe coercion helpers ─────────────────────────────────────────────────────
def _s(value, default: str = "") -> str:
    """None-safe string coercion (handles None, numbers, lists, dicts)."""
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
    """Coerce messy LLM truthiness. Fixes bool('false') == True bug."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "match", "matched")
    return bool(value)


def _as_float(value, default: float = 0.0) -> float:
    """
    Coerce to float, never raising. Handles None, '8.5', '8.5/10', 'high',
    numeric types, and stray text. Extracts the first number if embedded.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return default
    if isinstance(value, str):
        import re
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            try:
                return float(m.group())
            except Exception:
                return default
    return default


def _as_list(value) -> list[str]:
    """Coerce to list[str]. A string becomes a single-item list (or split on ,)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            t = _s(item)
            if t:
                out.append(t[:MAX_ITEM_CHARS])
            if len(out) >= MAX_LIST_ITEMS:
                break
        return out
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p[:MAX_ITEM_CHARS] for p in parts if p][:MAX_LIST_ITEMS]
    t = _s(value)
    return [t[:MAX_ITEM_CHARS]] if t else []


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class ICPResult:
    icp_match: bool = False
    icp_score: float = 0.0
    matched_criteria: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    major_disqualifiers: list[str] = field(default_factory=list)
    decision_maker_score: float = 0.0
    decision_maker_reasoning: str = ""
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "icp_match":                self.icp_match,
            "icp_score":                self.icp_score,
            "matched_criteria":         self.matched_criteria,
            "mismatches":               self.mismatches,
            "major_disqualifiers":      self.major_disqualifiers,
            "decision_maker_score":     self.decision_maker_score,
            "decision_maker_reasoning": self.decision_maker_reasoning,
        }


# ── Public API ────────────────────────────────────────────────────────────────
def validate_icp(lead_data: dict, icp_prompt: str,
                 job_id: str = None, lead_id: str = None) -> ICPResult:
    """
    Run Stage 1 ICP validation. Never raises — safe default on any failure.
    """
    if not isinstance(lead_data, dict):
        logger.warning("validate_icp: lead_data is not a dict — skipping")
        return _safe_default("Invalid lead data")

    icp_prompt = _s(icp_prompt)
    if not icp_prompt:
        logger.warning("validate_icp: empty icp_prompt")
        return _safe_default("No ICP prompt provided")

    prompt = _build_prompt(lead_data, icp_prompt)

    try:
        raw = llm_json(prompt, system=SYSTEM_ICP_VALIDATOR,max_output_tokens=600,
                        stage="icp", module="icp_validator",
                        job_id=job_id, lead_id=lead_id)
        if not isinstance(raw, dict):
            logger.warning(f"validate_icp: LLM returned non-dict ({type(raw)})")
            raw = {}
        result = _parse_response(raw)
        try:
            result.raw_output = json.dumps(raw, default=str)
        except Exception:
            result.raw_output = "{}"
        logger.debug(
            f"ICP [{_s(lead_data.get('Contact Name')) or '?'}] "
            f"match={result.icp_match} icp={result.icp_score} "
            f"dm={result.decision_maker_score} "
            f"disqualifiers={len(result.major_disqualifiers)}"
        )
        return result
    except Exception as e:
        logger.error(
            f"ICP validation failed for '{_s(lead_data.get('Contact Name')) or '?'}': {e}"
        )
        return _safe_default(str(e))


# ── Prompt builder ────────────────────────────────────────────────────────────
def _build_prompt(lead_data: dict, icp_prompt: str) -> str:
    # Only include non-empty fields — None-safe.
    ICP_RELEVANT = {
        "Contact Name", "Job Title", "Contact Seniority", "Contact Department",
        "Company Name", "Industry", "Sub Industry", "Employee Count",
        "Annual Revenue", "Company Country", "Company State/Region",
        "Email Address", "Phone Number", "LinkedIn Profile URL",
    }
    filled = {
        k: _s(v)[:120]
        for k, v in lead_data.items()
        if _s(v) and k in ICP_RELEVANT
    }
    lead_block = "\n".join(f"  {k}: {v}" for k, v in filled.items()) or "  (no data)"

    return f"""
You are evaluating a B2B sales lead against an Ideal Customer Profile (ICP).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ICP DEFINITION (provided by the sales team):
{icp_prompt}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LEAD DATA:
{lead_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVALUATION SCOPE — EVALUATE ONLY THESE FIVE CRITERIA. NOTHING ELSE.

  1. INDUSTRY        — matches the industry rule in the ICP.
  2. EMPLOYEE COUNT  — within the ICP's employee-count range.
  3. ANNUAL REVENUE  — within the ICP's revenue range.
  4. LOCATION        — within the ICP's target geography.
  5. SENIORITY       — the contact is a DECISION-MAKER (see below).
  6. CONTACT METHOD  — at least ONE valid contact: email OR phone
                       (email preferred; a valid email alone is sufficient).

CRITICAL — DO NOT INVENT CRITERIA. Judge ONLY the six items above. Do NOT
evaluate, reward, or penalise anything else — e.g. non-profit vs for-profit
status, ownership type, technologies used, growth signals, funding, tenure,
department, or any attribute NOT listed above. Such attributes must NEVER
appear in matched_criteria, mismatches, or major_disqualifiers.

SENIORITY RULE (criterion 5):
  - "Decision-maker" = ANY of: C-Level, Senior Executive, VP, Director, or
    Manager (per the CLASSIFICATION FRAMEWORK). Reason about the title.
  - If the Job Title clearly maps to any of these tiers → seniority MATCHES.
  - If the Job Title is MISSING, blank, or genuinely ambiguous → mark it as
    "seniority_unknown" in mismatches, but DO NOT treat it as a disqualifier
    and DO NOT heavily penalise the score for missing data.
  - Only put seniority in major_disqualifiers when the title CLEARLY identifies
    a NON-decision-maker individual contributor (e.g. Analyst, Engineer,
    Coordinator, Associate, Intern) — never for missing/unspecified titles.

CONTACT RULE (criterion 6):
  - A valid email OR a valid phone satisfies this — email is preferred and a
    valid email alone is fully sufficient.
  - Only add "missing_all_valid_contact_methods" to major_disqualifiers when
    BOTH email and phone are missing/invalid. A missing phone with a valid
    email is NOT a disqualifier, and vice-versa.

INSTRUCTIONS:
1. Assess each of the six criteria above using the LEAD DATA. Be semantic —
   handle typos, abbreviations, synonyms, and industry variations.
2. matched_criteria: list ONLY which of the six criteria the lead satisfies,
   using these exact labels where applicable: "industry", "employee_count",
   "annual_revenue", "location", "seniority", "contact_method".
3. mismatches: list ONLY which of the six criteria the lead does NOT satisfy
   or where the data is unknown (use the same labels; use "seniority_unknown"
   for a missing/ambiguous title).
4. major_disqualifiers: only HARD failures among the six — for example
   "non_target_industry", "employee_count_out_of_range", "revenue_out_of_range",
   "outside_target_geography", "non_decision_making_individual_contributor"
   (only for a CLEAR individual contributor), or
   "missing_all_valid_contact_methods" (only when BOTH email AND phone missing).
   Do NOT add disqualifiers for anything outside the six criteria, and do NOT
   disqualify for missing/unspecified seniority.
5. Score decision_maker_score using the AUTHORITY SCORE tiers.
6. If major_disqualifiers is not empty:
   - icp_match MUST be false.
   - icp_score MUST be below 6.0.
7. Otherwise, icp_match = true only if icp_score >= 6.0.

Return this exact JSON structure (no extra keys, no markdown):
{{
  "icp_match":                <true|false>,
  "icp_score":                <float 0.0-10.0>,
  "matched_criteria":         [<string>, ...],
  "mismatches":               [<string>, ...],
  "major_disqualifiers":      [<string>, ...],
  "decision_maker_score":     <float 0.0-10.0>,
  "decision_maker_reasoning": "<one sentence: state the seniority tier you
     classified the title into and why it is/ isn't a decision-maker>"
}}
""".strip()



# ── Response parser (fully type-safe) ─────────────────────────────────────────
def _parse_response(raw: dict) -> ICPResult:
    icp_score = _clamp(_as_float(raw.get("icp_score")), 0.0, 10.0)
    dm_score = _clamp(_as_float(raw.get("decision_maker_score")), 0.0, 10.0)
    major_disqualifiers = _as_list(raw.get("major_disqualifiers"))

    # Mandatory ICP failures override the model's score and match flag.
    if major_disqualifiers:
        icp_score = min(icp_score, ICP_MATCH_THRESHOLD - 0.1)

    icp_match = (
        icp_score >= ICP_MATCH_THRESHOLD
        and not major_disqualifiers
    )

    return ICPResult(
        icp_match=icp_match,
        icp_score=icp_score,
        matched_criteria=_as_list(raw.get("matched_criteria")),
        mismatches=_as_list(raw.get("mismatches")),
        major_disqualifiers=major_disqualifiers,
        decision_maker_score=dm_score,
        decision_maker_reasoning=_s(
            raw.get("decision_maker_reasoning")
        )[:MAX_REASON_CHARS],
    )


def _safe_default(error_msg: str) -> ICPResult:
    r = ICPResult(
        icp_match=False,
        icp_score=0.0,
        matched_criteria=[],
        mismatches=["Validation failed — LLM error"],
        major_disqualifiers=[],
        decision_maker_score=0.0,
        decision_maker_reasoning=f"Error: {_s(error_msg)[:MAX_REASON_CHARS]}",
    )
    try:
        r.raw_output = json.dumps({"error": _s(error_msg)})
    except Exception:
        r.raw_output = "{}"
    return r
