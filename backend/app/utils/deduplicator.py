"""
utils/deduplicator.py
──────────────────────
Removes duplicate rows from the parsed DataFrame before processing.

Deduplication strategy (in order, first match wins):
  1. Exact CRM Contact ID match (crm_contact_id) — strongest signal
  2. Exact email address match (case-insensitive)
  3. Normalized exact name + company match — catches casing/punctuation variants

The first occurrence of each duplicate group is kept; subsequent ones
are dropped and logged.

Import pattern:
    from app.utils.deduplicator import deduplicate

    df, stats = deduplicate(df)
    # stats: {"original": int, "duplicates_removed": int, "final": int}
"""

import logging
import pandas as pd
import re

logger = logging.getLogger(__name__)

def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Remove duplicate lead rows from a 29-column DataFrame.

    Returns:
        (deduplicated_df, stats_dict)
        stats_dict has keys: original, duplicates_removed, final
    """
    original_count = len(df)
    keep = [True] * len(df)   # True = keep this row

    # ── Pass 1: CRM Contact ID ────────────────────────────────────────────────
    if "CRM Contact ID" in df.columns:
        seen_ids: set[str] = set()
        for i, val in enumerate(df["CRM Contact ID"]):
            v = str(val).strip().lower()
            if v and v not in ("", "nan", "none"):
                if v in seen_ids:
                    keep[i] = False
                else:
                    seen_ids.add(v)

    # ── Pass 2: Email address ─────────────────────────────────────────────────
    if "Email Address" in df.columns:
        seen_emails: set[str] = set()
        for i, val in enumerate(df["Email Address"]):
            if not keep[i]:
                continue
            v = str(val).strip().lower()
            if v and v not in ("", "nan", "none"):
                if v in seen_emails:
                    keep[i] = False
                else:
                    seen_emails.add(v)

    # ── Pass 3: normalized exact name + company ──────────────────────────────
    name_col    = "Contact Name"    if "Contact Name"    in df.columns else None
    company_col = "Company Name"    if "Company Name"    in df.columns else None

    if name_col and company_col:
        seen_people: set[str] = set()

        for i in range(len(df)):
            if not keep[i]:
                continue

            name    = str(df.iloc[i][name_col]).strip().lower()
            company = str(df.iloc[i][company_col]).strip().lower()

            if not name and not company:
                continue

            norm_name = _normalise(name)
            norm_company = _normalise(company)
            signature = f"{norm_name}|{norm_company}"
            if signature in seen_people:
                keep[i] = False
            else:
                seen_people.add(signature)

    # ── Apply mask ────────────────────────────────────────────────────────────
    clean_df = df[keep].reset_index(drop=True)
    removed  = original_count - len(clean_df)

    if removed > 0:
        logger.info(f"Deduplicator: removed {removed} duplicates ({original_count} → {len(clean_df)} rows)")

    stats = {
        "original":           original_count,
        "duplicates_removed": removed,
        "final":              len(clean_df),
    }
    return clean_df, stats


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())
