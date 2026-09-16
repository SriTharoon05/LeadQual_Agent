"""
export/result_builder.py
─────────────────────────
Builds the enriched output files from the database after all leads
in a job have been processed.

Outputs:
  1. .xlsx  — 2-row merged group header (row 1) + column header (row 2)
              29 canonical columns + 10 extra score/validation columns
  2. .csv   — flat single header, same columns

Both files are uploaded to Azure Blob Storage and a download URL is returned.

Import pattern:
    from app.export.result_builder import build_export

    excel_url, csv_url = build_export(job_id, db)
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from azure.storage.blob import BlobServiceClient, ContentSettings
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload, selectinload

from app.config import (
    CANONICAL_COLUMNS,
    COLUMN_GROUPS,
    EXPORT_EXTRA_COLUMNS,
    settings,
)
from app.models import Lead
from app.utils.lead_identity import json_object
from app.validators.evidence import proof_rows

logger = logging.getLogger(__name__)

# All output columns in order
ALL_COLUMNS: list[str] = CANONICAL_COLUMNS + EXPORT_EXTRA_COLUMNS + [
    "Reviewed", "Tracking Notes", "Reuse Type", "Reused From Lead ID", "ICP Input",
    "Evidence Checked At", "Current Employment", "ICP Validation Proofs",
]

# Header style constants
_HDR1_FILL  = PatternFill("solid", fgColor="1F4E79")   # dark blue — group row
_HDR2_FILL  = PatternFill("solid", fgColor="2E75B6")   # mid blue — column row
_HDR1_FONT  = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
_HDR2_FONT  = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
_HIGH_FILL  = PatternFill("solid", fgColor="C6EFCE")   # green  — High priority
_MED_FILL   = PatternFill("solid", fgColor="FFEB9C")   # yellow — Medium priority
_LOW_FILL   = PatternFill("solid", fgColor="FFC7CE")   # red    — Low priority


# ── Public API ────────────────────────────────────────────────────────────────
def build_export(job_id: str, db: Session) -> tuple[str, str]:
    """
    Build Excel + CSV exports for a completed job and upload to Azure Blob.

    Returns:
        (excel_blob_url, csv_blob_url)
    """
    rows = _fetch_rows(job_id, db)
    if not rows:
        raise ValueError(f"No processed leads found for job {job_id}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # ── Excel ─────────────────────────────────────────────────────────────────
    excel_bytes = _build_excel(rows)
    excel_name  = f"exports/{job_id}/{timestamp}_results.xlsx"
    excel_url   = _upload_blob(excel_bytes, excel_name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ── CSV ───────────────────────────────────────────────────────────────────
    csv_bytes = _build_csv(rows)
    csv_name  = f"exports/{job_id}/{timestamp}_results.csv"
    csv_url   = _upload_blob(csv_bytes, csv_name, "text/csv")

    logger.info(f"Export complete for job {job_id}: {len(rows)} rows → Excel + CSV")
    return excel_url, csv_url


# ── Data fetch ────────────────────────────────────────────────────────────────
def _fetch_rows(job_id: str, db: Session) -> list[dict]:
    """Pull all leads + scores + validation results for a job into flat dicts."""
    leads = (
        db.query(Lead)
        .filter(Lead.job_id == job_id)
        .options(joinedload(Lead.job), joinedload(Lead.final_score), selectinload(Lead.validation_results))
        .order_by(Lead.row_index)
        .all()
    )

    rows: list[dict] = []
    for lead in leads:
        row: dict = {
            # 29 canonical columns
            "Contact Name":        lead.contact_name or "",
            "Job Title":           lead.job_title or "",
            "Contact Department":  lead.contact_department or "",
            "Contact Seniority":   lead.contact_seniority or "",
            "LinkedIn Profile URL":lead.linkedin_profile_url or "",
            "Email Address":       lead.email_address or "",
            "Phone Number":        lead.phone_number or "",
            "Direct Phone Number": lead.direct_phone_number or "",
            "Contact Country":     lead.contact_country or "",
            "Contact State/Region":lead.contact_state_region or "",
            "Contact City":        lead.contact_city or "",
            "Company Name":        lead.company_name or "",
            "Company Website":     lead.company_website or "",
            "Company LinkedIn URL":lead.company_linkedin_url or "",
            "Industry":            lead.industry or "",
            "Sub Industry":        lead.sub_industry or "",
            "Employee Count":      lead.employee_count or "",
            "Annual Revenue":      lead.annual_revenue or "",
            "Company Description": lead.company_description or "",
            "Technologies Used":   lead.technologies_used or "",
            "Company Country":     lead.company_country or "",
            "Company State/Region":lead.company_state_region or "",
            "Company City":        lead.company_city or "",
            "Company HQ Location": lead.company_hq_location or "",
            "HQ Phone Number":     lead.hq_phone_number or "",
            "CRM Account ID":      lead.crm_account_id or "",
            "CRM Contact ID":      lead.crm_contact_id or "",
            "Lead Source":         lead.lead_source or "",
            "Notes":               lead.notes or "",
            # Extra columns — defaults
            "ICP Score":           "",
            "Decision Maker Score":"",
            "Web Validation Score":"",
            "Contact Quality Score":"",
            "Final Score":         "",
            "Priority":            "",
            "Reasoning":           "",
            "Growth Signals":      "",
            "Outreach Intelligence":"",
            "Source URLs":         "",
            "Validation Status":   "Pending",
        }

        # Overlay final_scores
        if lead.final_score:
            fs = lead.final_score
            row["Final Score"]    = _fmt_score(fs.total_score)
            row["ICP Score"]      = _fmt_score(fs.icp_score)
            row["Decision Maker Score"] = _fmt_score(fs.authority_score)
            row["Reasoning"]      = fs.reasoning or ""
            row["Growth Signals"] = fs.growth_signals or ""
            row["Outreach Intelligence"] = fs.outreach_intelligence or ""
            row["Priority"]       = fs.priority or ""

        # Overlay per-stage validation results
        all_urls: list[str] = []
        for vr in lead.validation_results:
            if vr.stage == "web":
                row["Web Validation Score"] = _fmt_score(vr.score)
                if vr.sources_json:
                    try:
                        all_urls.extend(json.loads(vr.sources_json))
                    except Exception:
                        pass
            elif vr.stage == "contact":
                row["Contact Quality Score"] = _fmt_score(vr.score)
                row["Validation Status"] = "Passed" if vr.passed else "Failed"

        row["Source URLs"] = " | ".join(all_urls[:10])   # cap at 10 URLs

        web = next((v for v in lead.validation_results if v.stage == "web"), None)
        icp = next((v for v in lead.validation_results if v.stage == "icp"), None)
        web_data = json_object(web.raw_output) if web else {}
        proofs = proof_rows(web_data, json_object(icp.raw_output) if icp else {})
        row.update({
            "Reviewed": "Yes" if lead.tracking and lead.tracking.reviewed else "No",
            "Tracking Notes": lead.tracking.notes or "" if lead.tracking else "",
            "Reuse Type": lead.reuse_kind or "New",
            "Reused From Lead ID": str(lead.reused_from_lead_id or ""),
            "ICP Input": lead.job.icp_prompt or "",
            "Evidence Checked At": web_data.get("checked_at", ""),
            "Current Employment": web_data.get("employment_status", "unclear"),
            "ICP Validation Proofs": "\n\n".join(
                f"{p['label']} | ICP: {p['requirement']} | Finding: {p['value']} | {p['verdict']}\n"
                + " | ".join(source["url"] for source in p["sources"])
                for p in proofs) or "Evidence unavailable for this earlier result",
        })
        rows.append(row)

    return rows


# ── Excel builder ─────────────────────────────────────────────────────────────
def _build_excel(rows: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Leads"

    # ── Row 1: group headers (merged cells) ───────────────────────────────────
    col_idx = 1
    for group_name, group_cols in COLUMN_GROUPS.items():
        start = col_idx
        end   = col_idx + len(group_cols) - 1
        ws.merge_cells(
            start_row=1, start_column=start,
            end_row=1,   end_column=end,
        )
        cell = ws.cell(row=1, column=start, value=group_name)
        cell.fill      = _HDR1_FILL
        cell.font      = _HDR1_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        col_idx = end + 1

    # Extra score columns — one merged group
    extra_start = len(CANONICAL_COLUMNS) + 1
    extra_end   = len(ALL_COLUMNS)
    ws.merge_cells(
        start_row=1, start_column=extra_start,
        end_row=1,   end_column=extra_end,
    )
    cell = ws.cell(row=1, column=extra_start, value="AI Qualification Scores")
    cell.fill      = PatternFill("solid", fgColor="375623")
    cell.font      = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    cell.alignment = Alignment(horizontal="center", vertical="center")

    # ── Row 2: column headers ─────────────────────────────────────────────────
    for col_num, col_name in enumerate(ALL_COLUMNS, start=1):
        cell = ws.cell(row=2, column=col_num, value=col_name)
        cell.fill      = _HDR2_FILL
        cell.font      = _HDR2_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # ── Rows 3+: data ─────────────────────────────────────────────────────────
    for row_num, row_data in enumerate(rows, start=3):
        priority = row_data.get("Priority", "")
        row_fill = (
            _HIGH_FILL if priority == "High"
            else _MED_FILL if priority == "Medium"
            else _LOW_FILL if priority == "Low"
            else None
        )

        for col_num, col_name in enumerate(ALL_COLUMNS, start=1):
            cell = ws.cell(row=row_num, column=col_num, value=_safe_cell(row_data.get(col_name, "")))
            if row_fill:
                cell.fill = row_fill

    # ── Column widths ─────────────────────────────────────────────────────────
    _WIDTH_MAP = {
        "Contact Name": 22, "Job Title": 28, "Email Address": 30,
        "Company Name": 25, "Company Website": 30, "Company Description": 50,
        "Technologies Used": 40, "Reasoning": 55, "Growth Signals": 45,
        "Outreach Intelligence": 60,
        "Source URLs": 60,
        "Tracking Notes": 45, "ICP Input": 55, "ICP Validation Proofs": 70,
    }
    default_width = 18
    for col_num, col_name in enumerate(ALL_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_num)].width = (
            _WIDTH_MAP.get(col_name, default_width)
        )

    # Freeze panes at row 3 col 1 (scroll past headers)
    ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ── CSV builder ───────────────────────────────────────────────────────────────
def _build_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ALL_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _safe_cell(value) for key, value in row.items()} for row in rows)
    return buf.getvalue().encode("utf-8-sig")   # BOM for Excel compatibility


# ── Azure Blob upload — auto-creates container ────────────────────────────────
def _upload_blob(data: bytes, blob_name: str, content_type: str) -> str:
    service_client = BlobServiceClient.from_connection_string(
        settings.AZURE_BLOB_CONNECTION_STRING
    )
    container_name   = settings.AZURE_BLOB_CONTAINER
    container_client = service_client.get_container_client(container_name)

    # Create container if it doesn't exist (fixes ContainerNotFound)
    try:
        container_client.get_container_properties()
    except Exception:
        try:
            container_client.create_container()
            logger.info(f"Azure Blob container '{container_name}' created")
        except Exception as e:
            logger.warning(f"Container create: {e} — continuing")

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return blob_client.url

def build_excel_export(job_id: str, db: Session) -> str:
    rows = _fetch_rows(job_id, db)
    if not rows:
        raise ValueError(f"No processed leads found for job {job_id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _upload_blob(
        _build_excel(rows),
        f"exports/{job_id}/{timestamp}_results.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def build_csv_export(job_id: str, db: Session) -> str:
    rows = _fetch_rows(job_id, db)
    if not rows:
        raise ValueError(f"No processed leads found for job {job_id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _upload_blob(
        _build_csv(rows),
        f"exports/{job_id}/{timestamp}_results.csv",
        "text/csv",
    )

def build_excel_bytes(job_id: str, db: Session) -> bytes:
    """Build the enriched .xlsx and return raw bytes (no Blob upload)."""
    rows = _fetch_rows(job_id, db)
    if not rows:
        raise ValueError(f"No processed leads found for job {job_id}")
    return _build_excel(rows)


def build_csv_bytes(job_id: str, db: Session) -> bytes:
    """Build the enriched .csv and return raw bytes (no Blob upload)."""
    rows = _fetch_rows(job_id, db)
    if not rows:
        raise ValueError(f"No processed leads found for job {job_id}")
    return _build_csv(rows)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt_score(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{v:.1f}"


def _safe_cell(value):
    """Keep user-authored notes and uploaded values as text, never formulas."""
    if isinstance(value, str):
        # XML 1.0 cannot encode these controls in an XLSX cell.
        value = "".join(c for c in value if c in "\n\r\t" or ord(c) >= 32)
        if value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
    return value
