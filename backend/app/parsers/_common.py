"""
parsers/_common.py
──────────────────
Shared cleaning logic for both the Excel and CSV parsers.

Previously `_clean` lived in excel_parser.py and csv_parser.py imported it from
there — an awkward coupling. It now lives here so both parsers depend on a
neutral shared module instead of each other.

Import pattern:
    from app.parsers._common import clean_dataframe
"""

import logging

import pandas as pd

from app.config import LEAD_SOURCE_DEFAULT, settings
from app.utils.column_mapper import map_columns, apply_mapping

logger = logging.getLogger(__name__)


def clean_dataframe(raw_df: pd.DataFrame, source_label: str, job_id: str = None) -> pd.DataFrame:  # ← add
    if raw_df.empty:
        raise ValueError(f"{source_label} file is empty.")

    raw_df.columns = [str(c).strip() for c in raw_df.columns]
    raw_df = raw_df.dropna(how="all").reset_index(drop=True)

    if raw_df.empty:
        raise ValueError(f"{source_label} file has no data rows after dropping empties.")
    if len(raw_df) > settings.MAX_ROWS_PER_UPLOAD:
        raise ValueError(
            f"File contains {len(raw_df):,} rows; the configured limit is "
            f"{settings.MAX_ROWS_PER_UPLOAD:,}. Split the file or raise MAX_ROWS_PER_UPLOAD."
        )

    logger.info(f"[{source_label}] {len(raw_df)} rows, {len(raw_df.columns)} columns detected")

    mapping = map_columns(raw_df.columns.tolist(), sample_df=raw_df, job_id=job_id)  # ← pass it
    df = apply_mapping(raw_df, mapping)

    if "Lead Source" in df.columns:
        df["Lead Source"] = df["Lead Source"].replace("", LEAD_SOURCE_DEFAULT)
    else:
        df["Lead Source"] = LEAD_SOURCE_DEFAULT

    df = df.fillna("").astype(str)
    df = df.apply(lambda col: col.str.strip())
    df = df.replace({"nan": "", "None": "", "NaN": ""})

    logger.info(f"[{source_label}] Parsed → {len(df)} leads ready")
    return df
