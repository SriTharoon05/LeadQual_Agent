"""
utils/column_mapper.py
──────────────────────
Dynamic, LLM-first column mapper for production use.

Strategy (in order):
  1. Exact match  — instant, zero cost, handles known CRM sources
  2. LLM mapping  — sends ALL unmatched column headers + up to 3 sample
                    data rows so the model can read actual cell values
                    before deciding the canonical mapping.
                    One batched call covers ALL unresolved columns.
  3. Fuzzy match  — difflib safety net applied AFTER LLM, only to correct
                    hallucinated output values (not as a mapping strategy)

Why sample rows matter:
  A column called "Field_47", "Info2", or "Exec_Detail" tells you nothing.
  But if the first three values are "mike@acme.com", "j.doe@corp.io",
  "sarah@techco.com" — it's obviously an email column.
  The LLM reads both the header AND the values before mapping.

Special tokens:
  _FIRST_NAME  → combine with _LAST_NAME into "Contact Name"
  _LAST_NAME   → combine with _FIRST_NAME into "Contact Name"
  _IGNORE      → drop this column entirely

Import pattern:
    from app.utils.column_mapper import map_columns, apply_mapping

    mapping = map_columns(df.columns.tolist(), sample_df=df)
    df_clean = apply_mapping(df, mapping)
"""

import json
import logging
import re
from difflib import get_close_matches
from typing import Optional

import pandas as pd

from app.config import (
    CANONICAL_COLUMNS,
    COLUMN_MAPPING,
    _FIRST_NAME_KEY,
    _LAST_NAME_KEY,
    _IGNORE_KEY,
)
# FIX #1: use a dedicated column-mapping system prompt, NOT the ICP validator's.
from app.llm_engine import llm_json, SYSTEM_COLUMN_MAPPER

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_ROWS     = 3     # data rows sent to LLM per column
FUZZY_THRESHOLD = 0.82  # used only for post-LLM output correction
MAX_CELL_CHARS  = 120   # truncate long cell values before sending to LLM

# ── PII masking (applied to sample values BEFORE sending to the LLM) ──────────
# MASK_PII_SAMPLES  : mask emails + formatted phone numbers (safe, high value).
# MASK_NAMES        : also mask person-name-like tokens. OFF by default because
#                     names cannot be reliably distinguished from company/city/
#                     title values, so masking them can hurt mapping accuracy.
MASK_PII_SAMPLES = True
MASK_NAMES       = False

_EMAIL_RE        = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Phone: leading ( / digit / +, then digits + phone separators.
_PHONE_RE        = re.compile(r"^\+?[\d(][\d\s().\-]{5,}\d$")
# A phone MUST contain a formatting signal (+, space, -, (), .); this prevents a
# bare digit-run like "1500000" (revenue) or "94043" (zip) being masked.
_PHONE_FORMAT_RE = re.compile(r"[+\s().\-]")
_PHONE_KEEP_PREFIX = 4   # keep first 4 digits (country/area shape), mask the rest

_SOCIAL_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "youtube.com", "tiktok.com", "t.co",
    "pinterest.com", "snapchat.com",
}

def _is_social_url(url: str) -> bool:
    """Return True if the URL belongs to a known social media domain."""
    url = url.strip().lower()
    return any(domain in url for domain in _SOCIAL_DOMAINS)

def _looks_like_phone(v: str) -> bool:
    if not _PHONE_RE.match(v):
        return False
    if sum(c.isdigit() for c in v) < 7:
        return False
    if not _PHONE_FORMAT_RE.search(v):   # plain integer → not a phone
        return False
    return True


def _mask_email(v: str) -> str:
    local, _, domain = v.partition("@")
    if not local:
        return v
    return f"{local[0]}{'*' * max(2, len(local) - 1)}@{domain}"


def _mask_phone(v: str) -> str:
    seen, out = 0, []
    for ch in v:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen <= _PHONE_KEEP_PREFIX else "*")
        else:
            out.append(ch)   # keep +, spaces, -, (), .
    return "".join(out)


def _mask_name_token(tok: str) -> str:
    return tok if len(tok) <= 1 else tok[0] + "*" * (len(tok) - 1)


def _mask_pii(value: str) -> str:
    """
    Structure-preserving PII mask for a single sample value.

    - Emails  → m***@domain           (still obviously an email)
    - Phones  → +1 415 *** ****        (only if formatted; plain ints untouched)
    - Names   → C***** R******         (ONLY when MASK_NAMES is True)
    - Everything else (titles, companies, cities, revenue, ids) → untouched,
      so the LLM keeps the signal it needs to map the column correctly.
    """
    v = (value or "").strip()
    if not v:
        return v
    if _EMAIL_RE.match(v):
        return _mask_email(v)
    if _looks_like_phone(v):
        return _mask_phone(v)
    if MASK_NAMES:
        parts = v.split()
        if 1 <= len(parts) <= 4 and all(p[:1].isalpha() for p in parts):
            return " ".join(_mask_name_token(p) for p in parts)
    return v

# Pre-build O(1) exact lookup (lowercase stripped keys)
_EXACT_LOOKUP: dict[str, str] = {
    k.lower().strip(): v for k, v in COLUMN_MAPPING.items()
}

# Valid canonical output values the LLM is allowed to return
_VALID_OUTPUTS: set[str] = (
    set(CANONICAL_COLUMNS) | {_FIRST_NAME_KEY, _LAST_NAME_KEY, _IGNORE_KEY}
)


# ── Public API ────────────────────────────────────────────────────────────────
def map_columns(input_columns, sample_df=None, job_id=None) -> dict[str, str]:
    """
    Map input column headers to canonical column names.

    Args:
        input_columns: raw column header strings from the uploaded file.
        sample_df:     the raw DataFrame — used to extract sample cell values.
                       Always pass this; without it the LLM works blind.

    Returns:
        {original_header: canonical_name | _FIRST_NAME | _LAST_NAME | _IGNORE}
    """
    result:    dict[str, str] = {}
    llm_queue: list[str]      = []

    for col in input_columns:
        canonical = _try_exact(col)
        if canonical:
            result[col] = canonical
            logger.debug(f"Exact match  '{col}' → '{canonical}'")
        else:
            llm_queue.append(col)

    if llm_queue:
        samples     = _extract_samples(llm_queue, sample_df)
        llm_results = _llm_map(llm_queue, samples, job_id=job_id)

        for col, mapped in llm_results.items():
            if mapped in _VALID_OUTPUTS:
                result[col] = mapped
            else:
                # LLM returned something not in our schema — try to fuzzy-correct
                corrected = _fuzzy_correct(mapped)
                if corrected:
                    logger.warning(f"LLM returned '{mapped}' for '{col}' → corrected to '{corrected}'")
                    result[col] = corrected
                else:
                    logger.warning(f"LLM returned unrecognised '{mapped}' for '{col}' → _IGNORE")
                    result[col] = _IGNORE_KEY

    return result


def apply_mapping(
    df: pd.DataFrame,
    mapping: dict[str, str],
) -> pd.DataFrame:
    """
    Apply a column mapping to a DataFrame.

    - Renames mapped columns to their canonical names.
    - Combines _FIRST_NAME + _LAST_NAME into "Contact Name".
    - Drops _IGNORE columns.
    - COALESCES columns that collide on the same canonical name (keep-first-
      NON-EMPTY per row) instead of blindly dropping duplicates — so real data
      in a second column isn't silently lost. (FIX #2)
    - Adds missing canonical columns as empty strings.
    - Reorders to CANONICAL_COLUMNS order.
    """
    first_name_col: Optional[str] = None
    last_name_col:  Optional[str] = None
    ignore_cols:    list[str]     = []

    # canonical_name → list of ORIGINAL source columns mapped to it
    targets: dict[str, list[str]] = {}

    for original, canonical in mapping.items():
        if original not in df.columns:
            continue
        if canonical == _IGNORE_KEY:
            ignore_cols.append(original)
        elif canonical == _FIRST_NAME_KEY:
            first_name_col = original
        elif canonical == _LAST_NAME_KEY:
            last_name_col = original
        else:
            targets.setdefault(canonical, []).append(original)

    out = pd.DataFrame(index=df.index)

    # ── Coalesce each canonical target from its source column(s) ──────────────
    for canonical, sources in targets.items():
        series = _coalesce_columns(df, sources, canonical=canonical)
        if len(sources) > 1:
            logger.warning(
                f"Column collision on '{canonical}': {sources} — "
                f"coalesced (first non-empty per row wins)"
            )
        out[canonical] = series

    # ── Combine first + last name → Contact Name ─────────────────────────────
    if first_name_col or last_name_col:
        fn = (
            df[first_name_col].fillna("").astype(str)
            if first_name_col and first_name_col in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        ln = (
            df[last_name_col].fillna("").astype(str)
            if last_name_col and last_name_col in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        combined = (fn.str.strip() + " " + ln.str.strip()).str.strip()

        if "Contact Name" not in out.columns:
            out["Contact Name"] = combined
        else:
            # A full-name column already mapped; fill only where it's empty.
            existing = out["Contact Name"].astype(str).str.strip()
            mask = existing == ""
            out.loc[mask, "Contact Name"] = combined[mask]

    # ignore_cols are simply never copied into `out` (implicitly dropped).

    # ── Add missing canonical columns as empty strings ───────────────────────
    for col in CANONICAL_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    # ── Reorder to canonical order ───────────────────────────────────────────
    return out[CANONICAL_COLUMNS]


# ── Coalesce helper (keep-first-non-empty per row) ────────────────────────────
def _coalesce_columns(df: pd.DataFrame, sources: list[str],
                      canonical: str = "") -> pd.Series:
    """
    Merge source columns into one series — first non-empty per row wins.
    For Company Website: skip social media URLs entirely.
    """
    result = df[sources[0]].fillna("").astype(str).str.strip()
    for src in sources[1:]:
        col = df[src].fillna("").astype(str).str.strip()
        mask = result == ""
        result = result.where(~mask, col)

    # For Company Website — filter out any social URLs that slipped through
    if canonical == "Company Website":
        result = result.apply(
            lambda url: "" if _is_social_url(url) else url
        )

    return result


# ── LLM mapping ───────────────────────────────────────────────────────────────
def _llm_map(columns, samples, job_id=None) -> dict[str, str]:
    """
    Single LLM call that inspects column names AND sample cell values.
    Returns {original_col: canonical_name_or_token}.
    """
    col_blocks = []
    for col in columns:
        vals = samples.get(col, [])
        vals_str = " | ".join(f'"{v}"' for v in vals) if vals else "(no data in file)"
        col_blocks.append(f'  Column: "{col}"\n  Sample values: {vals_str}')

    col_section = "\n\n".join(col_blocks)

    prompt = f"""You are a data engineer mapping columns from an unknown CRM or data export
to a canonical B2B lead schema. Determine the correct mapping by reading
BOTH the column header name AND the actual sample values from the file.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANONICAL OUTPUT COLUMNS (use these exact strings as output values):
{json.dumps(CANONICAL_COLUMNS, indent=2)}

SPECIAL OUTPUT TOKENS:
  "_FIRST_NAME"  — column contains first name only (will be combined with last name)
  "_LAST_NAME"   — column contains last name only
  "_IGNORE"      — column has no place in the canonical schema; drop it

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COLUMNS TO MAP — header + sample cell values from the actual uploaded file:

{col_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES:
1. Sample values are the PRIMARY signal when the column name is ambiguous,
   generic, or in a foreign language.
   Examples:
     "Field_47"   + values ["mike@acme.com", "j@corp.io"]  → "Email Address"
     "Info_2"     + values ["+1 415 555 0101", "+44 20..."] → "Direct Phone Number"
     "Ejecutivo"  + values ["CEO", "CTO", "VP Engineering"] → "Job Title"
     "Nombre"     + values ["Carlos Ramirez", "Priya Patel"] → "Contact Name"

2. Handle any CRM source: ZoomInfo, Salesforce, HubSpot, Apollo, InsideView,
   EFX, Lusha, LinkedIn Sales Nav, Cognism, or any custom export.

3. Name splitting:
   - Column has first names only  → "_FIRST_NAME"
   - Column has last names only   → "_LAST_NAME"
   - Column has full names        → "Contact Name"
   - If both a full-name column AND separate first/last exist, map full-name → "_IGNORE"

4. ALWAYS map these to "_IGNORE" — no exceptions:
   - Parent company fields (parent employees, parent revenue, parent website, etc.)
   - Ultimate parent company fields
   - Internal CRM metadata (record IDs that aren't CRM Account/Contact ID)
   - Source-system quality/confidence scores (e.g. "Contact Quality Score",
     "Confidence Score", "Contact Accuracy Score" from the source system)
   - Social media follower counts, Alexa rank, internal codes
   - Social media profile URLs of ANY kind — Facebook, Twitter/X, Instagram,
     TikTok, YouTube, Blog URLs. These are NOT company websites.
     Examples: "Company Facebook Profile", "Company Twitter Profile",
     "Executive Twitter Profile", "Company Blog Profile" → ALL "_IGNORE"
   - "Company Website" maps ONLY to an actual corporate domain URL
     (e.g. https://www.acme.com). If sample values show facebook.com,
     twitter.com, linkedin.com, or any social domain → "_IGNORE"
   - Geographic area/territory/region labels for a person
     (e.g. "Executive Area", "Territory", "Sales Region") → "_IGNORE"
     These are NOT cities — only map to "Contact City" when values are
     clearly city names like "New York", "London", "Chennai"
   - Age, Salary, Education, Marital Status, personal demographics → "_IGNORE"
   - Fiscal year, quarter, SIC, NAICS, British SIC codes → "_IGNORE"
   - Employment status fields (Active, Terminated, etc.) → "_IGNORE"
   - Currency fields (Revenue Currency, Salary Currency) → "_IGNORE"
   - Count fields (Site Count, Location Count) → "_IGNORE"

5. Map to "CRM Account ID" if it looks like the source system's company identifier.
   Map to "CRM Contact ID" if it looks like the source system's person identifier.
   Examples: "EFX ID", "ZoomInfo Contact ID", "Salesforce Contact ID",
   "HubSpot Contact ID", "Apollo ID", "Executive ID" → "CRM Contact ID"

6. "Company Website" rule — STRICT:
   - Map to "Company Website" ONLY when sample values are real corporate URLs
     (e.g. https://www.acme.com, acme.io, acme.co.uk)
   - If the column is named "Website" but samples show social URLs → "_IGNORE"
   - NEVER map Facebook, Twitter, LinkedIn, Instagram, blog URLs to "Company Website"

7. Phone number disambiguation:
   - Direct/desk/office phone → "Direct Phone Number"
   - Mobile/cell phone → "Phone Number"
   - HQ/main company phone → "HQ Phone Number"
   - If unclear from name, use sample values to decide

8. If genuinely uncertain after reading both name and values → "_IGNORE"

9. Every input column key MUST appear in your response.
   Output values MUST be exactly one of the canonical strings or special tokens above.
   Do NOT invent new column names.

Return ONLY a valid JSON object — no markdown, no explanation:
{{
  "Column Header": "Canonical Column Name or _TOKEN",
  ...
}}"""
    
    try:
        # FIX #1: dedicated column-mapper system prompt (was SYSTEM_ICP_VALIDATOR)
        result = llm_json(prompt, system=SYSTEM_COLUMN_MAPPER,
                      stage="column_map", module="column_mapper",
                      job_id=job_id)

        mapped: dict[str, str] = {}
        for col in columns:
            val = result.get(col, _IGNORE_KEY)
            mapped[col] = val if val else _IGNORE_KEY
            logger.info(f"LLM mapped  '{col}' → '{mapped[col]}'")

        return mapped

    except Exception as e:
        logger.error(f"LLM column mapping failed: {e} — ignoring {len(columns)} columns")
        return {col: _IGNORE_KEY for col in columns}


# ── Sample data extractor ─────────────────────────────────────────────────────
def _extract_samples(
    columns: list[str],
    df: Optional[pd.DataFrame],
) -> dict[str, list[str]]:
    """
    Extract up to SAMPLE_ROWS non-empty cell values per column.
    Truncates long values to MAX_CELL_CHARS to keep the prompt lean.
    """
    if df is None or df.empty:
        return {col: [] for col in columns}

    samples: dict[str, list[str]] = {}
    for col in columns:
        if col not in df.columns:
            samples[col] = []
            continue

        col_series = df[col].dropna().astype(str).str.strip()
        # Filter out empty/null-like strings
        col_series = col_series[~col_series.isin(["", "nan", "None", "NaN", "none"])]
        vals = col_series.head(SAMPLE_ROWS).tolist()
        # Truncate first, then mask PII (email/phone) before it leaves the box.
        cleaned = [v[:MAX_CELL_CHARS] for v in vals]
        if MASK_PII_SAMPLES:
            cleaned = [_mask_pii(v) for v in cleaned]
        samples[col] = cleaned

    return samples


# ── Internal helpers ──────────────────────────────────────────────────────────
def _try_exact(col: str) -> Optional[str]:
    """O(1) exact match against the static COLUMN_MAPPING dict."""
    return _EXACT_LOOKUP.get(col.lower().strip())


def _fuzzy_correct(llm_output: str) -> Optional[str]:
    """
    Post-LLM sanity check: if the LLM returned something close to a
    canonical column name, correct it. Never used as a primary mapping strategy.
    """
    lower_canonicals = [c.lower() for c in CANONICAL_COLUMNS]
    matches = get_close_matches(
        llm_output.lower().strip(),
        lower_canonicals,
        n=1,
        cutoff=FUZZY_THRESHOLD,
    )
    if matches:
        idx = lower_canonicals.index(matches[0])
        return CANONICAL_COLUMNS[idx]
    return None
