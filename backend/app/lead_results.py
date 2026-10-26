"""Shared job lead responses, reviewed state, notes and evidence dates."""
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from sqlalchemy import case, func, select
from sqlalchemy.orm import load_only
from app.models import Lead, LeadRefresh
from app.utils.lead_identity import json_object
from app.validators.evidence import clean_sources, proof_rows
from app.validators.contact_policy import saved_contact

EVIDENCE_MAX_AGE_DAYS = 90


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def freshness(checked_at: datetime | None, now: datetime | None = None) -> tuple[str, int | None]:
    if not checked_at:
        return "unknown", None
    elapsed = utc(now or datetime.now(timezone.utc)) - utc(checked_at)
    if elapsed < timedelta(0):
        return "unknown", None
    return ("fresh" if elapsed <= timedelta(days=EVIDENCE_MAX_AGE_DAYS) else "stale", elapsed.days)


class LeadResultResponse(BaseModel):
    reviewed: bool = False
    tracking_notes: str = ""
    tracking_version: int = 0
    evidence_age_days: int | None = None
    evidence_status: Literal["fresh", "stale", "unknown"] = "unknown"
    refresh_status: Literal["queued", "running", "completed", "failed"] | None = None
    refresh_id: str | None = None
    refresh_error: str | None = None
    refresh_requested_at: datetime | None = None
    reuse_kind: str | None = None
    reused_from_lead_id: str | None = None
    icp_input: str | None = None
    icp_proofs: list[dict] = Field(default_factory=list)
    evidence_checked_at: datetime | None = None
    employment_status: str = "unclear"
    lead_id: str
    row_number: int | None = None
    contact_name: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    email_address: str | None = None
    final_score: float | None = None
    priority: str | None = None
    reasoning: str | None = None
    growth_signals: str | None = None
    outreach_intelligence: str | None = None
    icp_score: float | None = None
    growth_score: float | None = None
    contact_score: float | None = None
    qualification_status: str | None = None
    scoring_version: str | None = None
    scoring_details: dict = Field(default_factory=dict)
    source_urls: list[str] = Field(default_factory=list)
    company_description: str | None = None
    technologies_used: str | None = None


_LATEST_UNSET = object()


def latest_refreshes(db, lead_ids):
    """One current status per lead without loading every historical snapshot."""
    if not lead_ids:
        return {}
    ranked = select(LeadRefresh.id, func.row_number().over(
        partition_by=LeadRefresh.lead_id,
        order_by=(case((LeadRefresh.status.in_(("queued", "running")), 0), else_=1),
                  LeadRefresh.created_at.desc(), LeadRefresh.id.desc())).label("position")
    ).where(LeadRefresh.lead_id.in_(lead_ids)).subquery()
    rows = db.query(LeadRefresh).join(ranked, ranked.c.id == LeadRefresh.id).filter(
        ranked.c.position == 1).options(load_only(LeadRefresh.id, LeadRefresh.lead_id,
            LeadRefresh.status, LeadRefresh.error_message, LeadRefresh.created_at)).all()
    return {str(row.lead_id): row for row in rows}


def lead_to_response(lead: Lead, *, include_details=True, latest_refresh=_LATEST_UNSET) -> LeadResultResponse:
    score, tracking = lead.final_score, lead.tracking
    validations = (lead.validation_results or []) if include_details else []
    contact = next((value for value in validations if value.stage == "contact"), None)
    web = next((value for value in validations if value.stage == "web"), None)
    icp = next((value for value in validations if value.stage == "icp"), None)
    sources = []
    if web and web.sources_json:
        try:
            sources = json.loads(web.sources_json)
        except (ValueError, TypeError):
            pass
    web_data = json_object(web.raw_output) if web else {}
    icp_data = json_object(icp.raw_output) if icp else {}
    contact_data = saved_contact({"Email Address": lead.email_address, "Phone Number": lead.phone_number,
                                  "Direct Phone Number": lead.direct_phone_number},
                                 json_object(contact.raw_output) if contact else {}) if include_details else {}
    now = datetime.now(timezone.utc)
    evidence_status, age = freshness(lead.evidence_checked_at, now)
    if latest_refresh is _LATEST_UNSET:
        runs = lead.refresh_requests or []
        active = next((run for run in runs if run.status in {"queued", "running"}), None)
        latest = active or next(iter(runs), None)
    else:
        latest = latest_refresh
    return LeadResultResponse(
        reviewed=bool(tracking.reviewed) if tracking else False,
        tracking_notes=(tracking.notes or "") if tracking else "",
        tracking_version=tracking.version if tracking else 0,
        evidence_age_days=age, evidence_status=evidence_status,
        refresh_status=latest.status if latest else None,
        refresh_id=str(latest.id) if latest else None,
        refresh_error=latest.error_message if latest else None,
        refresh_requested_at=utc(latest.created_at) if latest else None,
        reuse_kind=lead.reuse_kind,
        reused_from_lead_id=str(lead.reused_from_lead_id) if lead.reused_from_lead_id else None,
        icp_input=lead.job.icp_prompt if include_details else None,
        icp_proofs=proof_rows(web_data, icp_data, contact_data, lead.job.icp_prompt) if include_details else [],
        evidence_checked_at=utc(lead.evidence_checked_at),
        employment_status=web_data.get("employment_status") if isinstance(web_data.get("employment_status"), str) and web_data.get("employment_status") in {"current", "former"} else "unclear",
        lead_id=str(lead.id), row_number=lead.row_index + 1 if lead.row_index is not None else None, contact_name=lead.contact_name,
        company_name=lead.company_name, job_title=lead.job_title,
        email_address=lead.email_address,
        final_score=score.total_score if score else None,
        priority=score.priority if score else None,
        reasoning=score.reasoning if score and include_details else None,
        growth_signals=score.growth_signals if score and include_details else None,
        outreach_intelligence=score.outreach_intelligence if score and include_details else None,
        icp_score=score.icp_score if score else None,
        growth_score=score.growth_score if score else None,
        contact_score=score.contact_score if score else None,
        qualification_status=score.qualification_status if score else None,
        scoring_version=score.scoring_version if score else None,
        scoring_details={key: value for key, value in json_object(score.scoring_details_json).items()
                         if key in {"weights", "icp", "contact", "growth", "reasons", "raw_weighted_score"}} if score and include_details else {},
        source_urls=clean_sources(sources), company_description=lead.company_description if include_details else None,
        technologies_used=lead.technologies_used if include_details else None,
    )
