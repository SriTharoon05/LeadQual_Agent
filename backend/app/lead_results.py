"""Shared lead response and workflow dates for job and workspace views."""
import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.models import Lead
from app.utils.lead_identity import json_object
from app.validators.evidence import clean_sources, proof_rows

EVIDENCE_MAX_AGE_DAYS = 90


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def follow_up_status(tracking, now: datetime | None = None) -> str:
    if not tracking or not tracking.follow_up_at:
        return "unscheduled"
    if tracking.follow_up_completed_at:
        return "completed"
    return "due" if utc(tracking.follow_up_at) <= utc(now or datetime.now(timezone.utc)) else "upcoming"


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
    owner_id: str | None = None
    owner_name: str | None = None
    owner_active: bool | None = None
    follow_up_at: datetime | None = None
    follow_up_note: str = ""
    follow_up_completed_at: datetime | None = None
    follow_up_status: Literal["due", "upcoming", "completed", "unscheduled"] = "unscheduled"
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
    authority_score: float | None = None
    data_score: float | None = None
    growth_score: float | None = None
    contact_score: float | None = None
    validation_status: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    company_description: str | None = None
    technologies_used: str | None = None


def lead_to_response(lead: Lead) -> LeadResultResponse:
    score, tracking = lead.final_score, lead.tracking
    validations = lead.validation_results or []
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
    owner = tracking.owner if tracking else None
    now = datetime.now(timezone.utc)
    evidence_status, age = freshness(lead.evidence_checked_at, now)
    runs = lead.refresh_requests or []
    active = next((run for run in runs if run.status in {"queued", "running"}), None)
    latest = active or next(iter(runs), None)
    return LeadResultResponse(
        reviewed=bool(tracking.reviewed) if tracking else False,
        tracking_notes=(tracking.notes or "") if tracking else "",
        tracking_version=tracking.version if tracking else 0,
        owner_id=str(tracking.owner_id) if tracking and tracking.owner_id else None,
        owner_name=owner.name if owner else None,
        owner_active=owner.active if owner else None,
        follow_up_at=utc(tracking.follow_up_at) if tracking else None,
        follow_up_note=(tracking.follow_up_note or "") if tracking else "",
        follow_up_completed_at=utc(tracking.follow_up_completed_at) if tracking else None,
        follow_up_status=follow_up_status(tracking, now),
        evidence_age_days=age, evidence_status=evidence_status,
        refresh_status=latest.status if latest else None,
        refresh_id=str(latest.id) if latest else None,
        refresh_error=latest.error_message if latest else None,
        refresh_requested_at=utc(latest.created_at) if latest else None,
        reuse_kind=lead.reuse_kind,
        reused_from_lead_id=str(lead.reused_from_lead_id) if lead.reused_from_lead_id else None,
        icp_input=lead.job.icp_prompt, icp_proofs=proof_rows(web_data, icp_data),
        evidence_checked_at=utc(lead.evidence_checked_at),
        employment_status=web_data.get("employment_status") if web_data.get("employment_status") in {"current", "former"} else "unclear",
        lead_id=str(lead.id), contact_name=lead.contact_name,
        company_name=lead.company_name, job_title=lead.job_title,
        email_address=lead.email_address,
        final_score=score.total_score if score else None,
        priority=score.priority if score else None,
        reasoning=score.reasoning if score else None,
        growth_signals=score.growth_signals if score else None,
        outreach_intelligence=score.outreach_intelligence if score else None,
        icp_score=score.icp_score if score else None,
        authority_score=score.authority_score if score else None,
        data_score=score.data_score if score else None,
        growth_score=score.growth_score if score else None,
        contact_score=score.contact_score if score else None,
        validation_status=("Passed" if contact.passed else "Failed") if contact else "Pending",
        source_urls=clean_sources(sources), company_description=lead.company_description,
        technologies_used=lead.technologies_used,
    )
