"""
parsers/csv_parser.py
─────────────────────
Reads .csv files, auto-detects encoding + delimiter, and returns a clean
29-column DataFrame. Passes the raw DataFrame to column_mapper (via
_common.clean_dataframe) so the LLM can inspect sample cell values before
deciding the mapping.
"""

import io
import logging
from typing import Union

import chardet
import pandas as pd

# No longer imports _clean from excel_parser — uses the shared _common module.
from app.parsers._common import clean_dataframe

logger = logging.getLogger(__name__)

_DELIMITER_CANDIDATES = [",", ";", "\t", "|"]


def parse_csv(source: Union[bytes, str, io.BytesIO], job_id: str = None) -> pd.DataFrame:  # ← add
    raw_bytes, encoding = _resolve_source(source)
    delimiter = _detect_delimiter(raw_bytes, encoding)
    logger.info(f"[CSV] Detected encoding={encoding}, delimiter={repr(delimiter)}")

    try:
        raw_df = pd.read_csv(
            io.BytesIO(raw_bytes),
            encoding=encoding,
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            on_bad_lines="warn",
        )
    except Exception as e:
        raise ValueError(f"Failed to read CSV file: {e}") from e

    return clean_dataframe(raw_df, source_label="CSV", job_id=job_id)  # ← pass it


def _resolve_source(source: Union[bytes, str, io.BytesIO]) -> tuple[bytes, str]:
    if isinstance(source, str):
        with open(source, "rb") as f:
            raw_bytes = f.read()
    elif isinstance(source, io.BytesIO):
        raw_bytes = source.read()
        source.seek(0)
    elif isinstance(source, bytes):
        raw_bytes = source
    else:
        raise ValueError(f"Unsupported source type: {type(source)}")

    detection = chardet.detect(raw_bytes[:50_000])
    encoding  = detection.get("encoding") or "utf-8"
    if encoding.lower() in ("ascii", "iso-8859-1", "latin-1"):
        encoding = "latin-1"

    return raw_bytes, encoding


def _detect_delimiter(raw_bytes: bytes, encoding: str) -> str:
    import csv
    try:
        sample  = raw_bytes[:4096].decode(encoding, errors="replace")
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITER_CANDIDATES))
        return dialect.delimiter
    except csv.Error:
        logger.warning("[CSV] Delimiter sniffing failed — defaulting to ','")
        return ","
