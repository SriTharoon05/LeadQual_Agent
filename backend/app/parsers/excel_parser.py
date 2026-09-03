"""
parsers/excel_parser.py
───────────────────────
Reads .xlsx / .xls / .xlsm files and returns a clean 29-column DataFrame.
Passes the raw DataFrame to column_mapper (via _common.clean_dataframe) so the
LLM can inspect sample cell values — not just column names — before mapping.
"""

import io
import logging
from typing import Union

import pandas as pd

from app.parsers._common import clean_dataframe

logger = logging.getLogger(__name__)


def parse_excel(
    source: Union[bytes, str, io.BytesIO],
    sheet_index: int = 0,
    job_id: str = None,       # ← add this
) -> pd.DataFrame:
    if isinstance(source, bytes):
        source = io.BytesIO(source)

    try:
        raw_df = pd.read_excel(
            source,
            sheet_name=sheet_index,
            header=0,
            dtype=str,
            keep_default_na=False,
        )
    except Exception as e:
        raise ValueError(f"Failed to read Excel file: {e}") from e

    return clean_dataframe(raw_df, source_label="Excel", job_id=job_id)  # ← pass it
