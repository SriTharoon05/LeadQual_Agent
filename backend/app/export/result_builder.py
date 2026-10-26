"""
export/result_builder.py
─────────────────────────
Builds the enriched output files from the database after all leads
in a job have been processed.

Outputs:
  1. .xlsx  — group header (row 1) + column header (row 2), written sequentially
              to disk; oversized text is preserved in continuation worksheets
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
import math
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from datetime import datetime, timezone
from typing import Optional

import xlsxwriter


from azure.storage.blob import BlobServiceClient, ContentSettings
from sqlalchemy.orm import Session
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import joinedload, selectinload, noload

from app.config import (
    CANONICAL_COLUMNS,
    COLUMN_GROUPS,
    EXPORT_EXTRA_COLUMNS,
    settings,
)
from app.database import SessionLocal
from app.models import FinalScore, Job, Lead, LeadRefresh, ValidationResult
from app.lead_results import freshness, utc
from app.utils.lead_identity import json_object
from app.validators.evidence import proof_rows
from app.validators.contact_policy import saved_contact

logger = logging.getLogger(__name__)

# All output columns in order
ALL_COLUMNS: list[str] = CANONICAL_COLUMNS + EXPORT_EXTRA_COLUMNS + [
    "Row Number", "Lead ID", "Processing Status", "Reviewed", "Tracking Notes", "Reuse Type", "Reused From Lead ID", "ICP Input",
    "Evidence Checked At", "Current Employment", "ICP Validation Proofs",
    "Evidence Age (Days)",
    "Evidence Status", "Refresh Status", "Export Warnings", "Export Snapshot At (UTC)",
]


def _lead_row(lead, now, refresh_status="") -> dict:
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
        "Contact Score":        "",
        "Growth Score":         "",
        "Final Score":         "",
        "Priority":            "",
        "Reasoning":           "",
        "Growth Signals":      "",
        "Outreach Intelligence":"",
        "Source URLs":         "",
        "Qualification Status": "",
        "Scoring Version":      "",
    }

    # Overlay final_scores
    if lead.final_score:
        fs = lead.final_score
        row["Final Score"]    = _fmt_score(fs.total_score)
        row["ICP Score"]      = _fmt_score(fs.icp_score)
        row["Contact Score"] = _fmt_score(fs.contact_score)
        row["Growth Score"] = _fmt_score(fs.growth_score)
        row["Qualification Status"] = fs.qualification_status or ""
        row["Scoring Version"] = fs.scoring_version or ""
        row["Reasoning"]      = fs.reasoning or ""
        row["Growth Signals"] = fs.growth_signals or ""
        row["Outreach Intelligence"] = fs.outreach_intelligence or ""
        row["Priority"]       = fs.priority or ""

    # Overlay per-stage validation results
    all_urls: list[str] = []
    for vr in lead.validation_results:
        if vr.stage == "web":
            if vr.sources_json:
                try:
                    sources = json.loads(vr.sources_json)
                    for source in (sources if isinstance(sources, list) else []):
                        url = source.get("url") if isinstance(source, dict) else source
                        if isinstance(url, str):
                            all_urls.append(url)
                except Exception:
                    pass

    web = next((v for v in lead.validation_results if v.stage == "web"), None)
    icp = next((v for v in lead.validation_results if v.stage == "icp"), None)
    web_data = json_object(web.raw_output) if web else {}
    for source in (web_data.get("sources") if isinstance(web_data.get("sources"), list) else []):
        url = source.get("url") if isinstance(source, dict) else source
        if isinstance(url, str):
            all_urls.append(url)
    row["Source URLs"] = " | ".join(dict.fromkeys(all_urls))
    contact = next((v for v in lead.validation_results if v.stage == "contact"), None)
    row["Export Warnings"] = ""
    try:
        contact_data = saved_contact(row, json_object(contact.raw_output) if contact else {})
        proofs = proof_rows(web_data, json_object(icp.raw_output) if icp else {}, contact_data, lead.job.icp_prompt)
        proof_text = "\n\n".join(_proof_text(proof) for proof in proofs)
    except (TypeError, ValueError, AttributeError, OverflowError):
        # One malformed legacy evidence payload must not discard an entire file.
        # Preserve the raw content visibly rather than inventing a proof or match.
        row["Export Warnings"] = "Legacy evidence could not be formatted; original saved evidence is retained in ICP Validation Proofs."
        proof_text = json.dumps({vr.stage: {"raw_output": vr.raw_output, "sources": vr.sources_json}
                                 for vr in lead.validation_results}, ensure_ascii=False, default=str)
        logger.warning("Export retained unformatted legacy evidence for lead %s", lead.id)
    tracking = lead.tracking
    evidence_status, evidence_age = freshness(lead.evidence_checked_at, now)
    row.update({
        "Reviewed": "Yes" if lead.tracking and lead.tracking.reviewed else "No",
        "Tracking Notes": lead.tracking.notes or "" if lead.tracking else "",
        "Reuse Type": lead.reuse_kind or "New",
        "Reused From Lead ID": str(lead.reused_from_lead_id or ""),
        "ICP Input": lead.job.icp_prompt or "",
        "Evidence Checked At": _fmt_date(lead.evidence_checked_at),
        "Current Employment": web_data.get("employment_status", "unclear"),
        "ICP Validation Proofs": proof_text or "Evidence unavailable for this earlier result",
        "Evidence Age (Days)": evidence_age if evidence_age is not None else "",
        "Evidence Status": evidence_status,
        "Refresh Status": refresh_status,
        "Export Snapshot At (UTC)": _fmt_date(now),
    })
    row.update({"Row Number": lead.row_index + 1 if lead.row_index is not None else "", "Lead ID": str(lead.id), "Processing Status": lead.processing_status})
    return row


def _proof_text(proof):
    sources = proof.get("sources")
    urls = [source["url"] for source in sources if isinstance(source, dict) and isinstance(source.get("url"), str)] if isinstance(sources, list) else []
    return (f"{proof.get('label', 'Evidence')} | ICP: {proof.get('requirement', 'Unclear')} | "
            f"Finding: {proof.get('value', 'Unclear')} | {proof.get('verdict', 'unclear')}\n" + " | ".join(urls))


def iter_rows(job_id: str, as_of: datetime, progress=None):
    """Bounded keyset pages; never materialize an upload or refresh history.

    Azure SQL orders uniqueidentifier values differently from Python; all cursor
    comparisons and ordering therefore stay in SQL, including nullable row IDs.
    Each page closes its connection before file writing resumes.
    """
    cursor = None
    count = 0
    while True:
        with SessionLocal() as db:
            query = db.query(Lead).filter(Lead.job_id == job_id).options(
                joinedload(Lead.job).load_only(Job.icp_prompt),
                joinedload(Lead.final_score).load_only(
                    FinalScore.total_score, FinalScore.icp_score, FinalScore.contact_score,
                    FinalScore.growth_score, FinalScore.priority, FinalScore.reasoning,
                    FinalScore.growth_signals, FinalScore.outreach_intelligence,
                    FinalScore.qualification_status, FinalScore.scoring_version),
                joinedload(Lead.tracking),
                selectinload(Lead.validation_results).load_only(
                    ValidationResult.stage, ValidationResult.raw_output, ValidationResult.sources_json),
                noload(Lead.refresh_requests),
            )
            if cursor is not None:
                row_index, lead_id = cursor
                if row_index is None:
                    query = query.filter(or_(
                        and_(Lead.row_index.is_(None), Lead.id > lead_id),
                        Lead.row_index.isnot(None)))
                else:
                    query = query.filter(or_(Lead.row_index > row_index,
                        and_(Lead.row_index == row_index, Lead.id > lead_id)))
            batch = query.order_by(Lead.row_index, Lead.id).limit(settings.EXPORT_BATCH_SIZE).all()
            if not batch:
                return
            ids = [lead.id for lead in batch]
            latest = select(LeadRefresh.lead_id, LeadRefresh.status, func.row_number().over(
                partition_by=LeadRefresh.lead_id,
                order_by=(case((LeadRefresh.status.in_(("queued", "running")), 0), else_=1),
                          LeadRefresh.created_at.desc(), LeadRefresh.id.desc())).label("position")
            ).where(LeadRefresh.lead_id.in_(ids)).subquery()
            refreshes = dict(db.execute(select(latest.c.lead_id, latest.c.status)
                                       .where(latest.c.position == 1)).all())
            rows = [_lead_row(lead, as_of, refreshes.get(lead.id, "")) for lead in batch]
            cursor = (batch[-1].row_index, batch[-1].id)
        yield from rows
        count += len(rows)
        if progress:
            progress(count)


_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _clean_text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return _INVALID_XML.sub("", value) if isinstance(value, str) else value


def _safe_cell(value):
    value = _clean_text(value)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _fmt_score(value: Optional[float]) -> float | str:
    return round(value, 1) if value is not None and math.isfinite(value) else ""


def _fmt_date(value: Optional[datetime]) -> str:
    return utc(value).isoformat().replace("+00:00", "Z") if value is not None else ""


def write_export(job_id: str, format: str, path: Path, as_of: datetime, progress=None) -> int:
    """Write only the requested format, directly to disk with bounded memory."""
    rows = iter_rows(job_id, as_of, progress)
    if format == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=ALL_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            count = 0
            for row in rows:
                writer.writerow({key: _safe_cell(value) for key, value in row.items()})
                count += 1
            return count
    if format != "excel":
        raise ValueError("Unsupported export format")
    return _write_excel(rows, path)


def _write_excel(rows, path: Path) -> int:
    # Sequential rows are flushed to disk; formula/URL auto-detection is disabled
    # for uploaded values, and long text is preserved in continuation sheets.
    with xlsxwriter.Workbook(str(path), {
        "constant_memory": True, "strings_to_formulas": False,
        "strings_to_urls": False, "use_zip64": True, "tmpdir": str(path.parent),
    }) as book:
        group_style = book.add_format({"bold": True, "bg_color": "#1F4E79", "font_color": "white"})
        header_style = book.add_format({"bold": True, "bg_color": "#2E75B6", "font_color": "white", "text_wrap": True})
        fills = {priority: book.add_format({"bg_color": color}) for priority, color in (
            ("High", "#C6EFCE"), ("Medium", "#FFEB9C"), ("Low", "#FFC7CE"))}
        score_fills = {priority: book.add_format({"bg_color": color, "num_format": "0.0"}) for priority, color in (
            ("High", "#C6EFCE"), ("Medium", "#FFEB9C"), ("Low", "#FFC7CE"))}
        plain_score = book.add_format({"num_format": "0.0"})
        widths = {"Reasoning": 55, "ICP Validation Proofs": 70, "ICP Input": 55,
                  "Outreach Intelligence": 60, "Source URLs": 60, "Tracking Notes": 45,
                  "Company Description": 50, "Growth Signals": 45, "Email Address": 32}

        def new_sheet(number):
            sheet = book.add_worksheet("Leads" if number == 1 else f"Leads_{number}")
            groups = [name for name, columns in COLUMN_GROUPS.items() for _ in columns]
            groups += ["Qualification and workflow"] * (len(ALL_COLUMNS) - len(groups))
            sheet.write_row(0, 0, groups, group_style)
            sheet.write_row(1, 0, ALL_COLUMNS, header_style)
            sheet.freeze_panes(2, 0)
            sheet.set_row(1, 32)
            for index, column in enumerate(ALL_COLUMNS):
                sheet.set_column(index, index, widths.get(column, 22))
            return sheet

        sheet_number, row_number, count = 1, 2, 0
        sheet = new_sheet(sheet_number)
        overflow, overflow_number, overflow_row = None, 0, 0
        for row in rows:
            if row_number >= 1_048_576:
                sheet.autofilter(1, 0, row_number - 1, len(ALL_COLUMNS) - 1)
                sheet_number += 1
                sheet, row_number = new_sheet(sheet_number), 2
            style = fills.get(row.get("Priority"))
            for column_number, column in enumerate(ALL_COLUMNS):
                cell_style = score_fills.get(row.get("Priority"), plain_score) if column.endswith("Score") else style
                value = _safe_cell(row.get(column, ""))
                if isinstance(value, str):
                    if len(value) > 16_000 and len(value.encode("utf-16-le")) > 64_000:
                        # 16,000 codepoints fit even if every character uses a
                        # surrogate pair. No notes/proofs disappear at Excel's limit.
                        continuation = ""
                        for part, start in enumerate(range(0, len(value), 16_000), 1):
                            if overflow is None or overflow_row >= 1_048_576:
                                overflow_number += 1
                                overflow = book.add_worksheet(f"LongText_{overflow_number}")
                                overflow.write_row(0, 0, ["Lead ID", "Row Number", "Column", "Part", "Text"], header_style)
                                overflow.set_column(4, 4, 100)
                                overflow_row = 1
                            if not continuation:
                                continuation = f"LongText_{overflow_number}, row {overflow_row + 1}"
                            overflow.write_row(overflow_row, 0, [row.get("Lead ID", ""), row.get("Row Number", ""), column,
                                                               part, value[start:start + 16_000]])
                            overflow_row += 1
                        value = value[:15_000] + f"\n[Full text: {continuation}; join parts in order.]"
                    sheet.write_string(row_number, column_number, value, cell_style)
                elif value is not None:
                    sheet.write(row_number, column_number, value, cell_style)
            row_number += 1
            count += 1
        sheet.autofilter(1, 0, max(1, row_number - 1), len(ALL_COLUMNS) - 1)
        return count


def _upload_file(path: Path, blob_name: str, content_type: str) -> str:
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
    with BlobServiceClient.from_connection_string(
            settings.AZURE_BLOB_CONNECTION_STRING, connection_timeout=10, read_timeout=60,
            retry_total=3) as service:
        container = service.get_container_client(settings.AZURE_BLOB_CONTAINER)
        try:
            container.get_container_properties()
        except ResourceNotFoundError:
            try:
                container.create_container()
            except ResourceExistsError:
                pass
        blob = container.get_blob_client(blob_name)
        with path.open("rb") as source:
            blob.upload_blob(source, length=path.stat().st_size, overwrite=True, max_concurrency=2,
                content_settings=ContentSettings(content_type=content_type, cache_control="private, no-store"))
        return blob.url


# Compatibility helpers for internal callers. HTTP exports use background tasks
# and disk files instead; these byte-returning helpers intentionally aren't routes.
def _bytes(job_id: str, format: str) -> bytes:
    with TemporaryDirectory(prefix="ql-export-") as directory:
        path = Path(directory) / ("results.csv" if format == "csv" else "results.xlsx")
        write_export(job_id, format, path, datetime.now(timezone.utc))
        return path.read_bytes()


def build_excel_bytes(job_id: str, db: Session) -> bytes:
    return _bytes(job_id, "excel")


def build_csv_bytes(job_id: str, db: Session) -> bytes:
    return _bytes(job_id, "csv")


def _build_blob(job_id: str, format: str) -> str:
    extension = "csv" if format == "csv" else "xlsx"
    content_type = "text/csv; charset=utf-8" if format == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    with TemporaryDirectory(prefix="ql-export-") as directory:
        path = Path(directory) / f"results.{extension}"
        write_export(job_id, format, path, datetime.now(timezone.utc))
        return _upload_file(path, f"exports/{job_id}/{uuid4()}/results.{extension}", content_type)


def build_excel_export(job_id: str, db: Session) -> str:
    return _build_blob(job_id, "excel")


def build_csv_export(job_id: str, db: Session) -> str:
    return _build_blob(job_id, "csv")


def build_export(job_id: str, db: Session) -> tuple[str, str]:
    return build_excel_export(job_id, db), build_csv_export(job_id, db)


def _build_excel(rows: list[dict]) -> bytes:
    with TemporaryDirectory(prefix="ql-export-") as directory:
        path = Path(directory) / "results.xlsx"
        _write_excel(rows, path)
        return path.read_bytes()


def _build_csv(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ALL_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _safe_cell(value) for key, value in row.items()} for row in rows)
    return output.getvalue().encode("utf-8-sig")


def _fetch_rows(job_id: str, db: Session) -> list[dict]:
    """Legacy in-process helper; the production exporter never uses this path."""
    leads = db.query(Lead).filter(Lead.job_id == job_id).options(
        joinedload(Lead.job), joinedload(Lead.final_score), joinedload(Lead.tracking),
        selectinload(Lead.validation_results), selectinload(Lead.refresh_requests)
    ).order_by(Lead.row_index, Lead.id).all()
    now = datetime.now(timezone.utc)
    return [_lead_row(lead, now, next((run.status for run in lead.refresh_requests
        if run.status in ("queued", "running")), lead.refresh_requests[0].status if lead.refresh_requests else ""))
        for lead in leads]
