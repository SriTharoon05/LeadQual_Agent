"""Validated lead filters shared by upload results and the sales workspace."""

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import or_

from app.models import FinalScore, Lead, LeadTracking


EVIDENCE_FRESH_DAYS = 90
FollowUpFilter = Literal["all", "due", "upcoming", "completed", "unscheduled"]
EvidenceFilter = Literal["all", "fresh", "stale", "unknown"]


class LeadFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: str = Field(default="", max_length=100)
    priority: Optional[Literal["High", "Medium", "Low"]] = None
    owner_id: Optional[UUID] = None
    unassigned: bool = False
    reviewed: Optional[bool] = None
    follow_up: FollowUpFilter = "all"
    country: str = Field(default="", max_length=100)
    industry: str = Field(default="", max_length=100)
    min_score: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    evidence: EvidenceFilter = "all"
    job_id: Optional[UUID] = None

    @field_validator("search", "country", "industry")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def check_owner(self):
        if self.unassigned and self.owner_id is not None:
            raise ValueError("Choose either an owner or unassigned leads, not both.")
        return self


def lead_filter_params(
    search: str = Query("", max_length=100),
    priority: Optional[Literal["High", "Medium", "Low"]] = Query(None),
    owner_id: Optional[UUID] = Query(None),
    unassigned: bool = Query(False),
    reviewed: Optional[bool] = Query(None),
    follow_up: FollowUpFilter = Query("all"),
    country: str = Query("", max_length=100),
    industry: str = Query("", max_length=100),
    min_score: Optional[float] = Query(None, ge=0, le=100, allow_inf_nan=False),
    evidence: EvidenceFilter = Query("all"),
    job_id: Optional[UUID] = Query(None),
) -> LeadFilters:
    try:
        return LeadFilters(
            search=search, priority=priority, owner_id=owner_id,
            unassigned=unassigned, reviewed=reviewed, follow_up=follow_up,
            country=country, industry=industry, min_score=min_score,
            evidence=evidence, job_id=job_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid lead filters.") from exc


def utc_now() -> datetime:
    """SQL Server DateTime values are naive UTC, never local wall-clock time."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sql_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def literal_contains(column, value: str):
    """Treat SQL Server LIKE metacharacters as user text, including brackets."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
    return column.ilike("%" + escaped + "%", escape="\\")


def reminder_predicate(status: FollowUpFilter, now: datetime):
    if status == "due":
        return (
            LeadTracking.follow_up_at.isnot(None),
            LeadTracking.follow_up_at <= now,
            LeadTracking.follow_up_completed_at.is_(None),
        )
    if status == "upcoming":
        return (
            LeadTracking.follow_up_at > now,
            LeadTracking.follow_up_completed_at.is_(None),
        )
    if status == "completed":
        return (LeadTracking.follow_up_completed_at.isnot(None),)
    if status == "unscheduled":
        return (
            LeadTracking.follow_up_at.is_(None),
            LeadTracking.follow_up_completed_at.is_(None),
        )
    return ()


def apply_filters(query, filters: LeadFilters, now: Optional[datetime] = None, *, joined: bool = False):
    """Apply rules to the complete SQL query before pagination.

    By default this adds the score and shared tracking joins. Callers whose
    base query already has both joins can explicitly pass joined=True.
    """
    now = _sql_now(now)
    if not joined:
        query = query.outerjoin(FinalScore, FinalScore.lead_id == Lead.id).outerjoin(
            LeadTracking, LeadTracking.id == Lead.tracking_id)
    if filters.job_id is not None:
        query = query.filter(Lead.job_id == str(filters.job_id))
    if filters.search:
        query = query.filter(or_(
            literal_contains(Lead.company_name, filters.search),
            literal_contains(Lead.contact_name, filters.search),
            literal_contains(Lead.email_address, filters.search),
            literal_contains(Lead.job_title, filters.search),
        ))
    if filters.priority is not None:
        query = query.filter(FinalScore.priority == filters.priority)
    if filters.owner_id is not None:
        query = query.filter(LeadTracking.owner_id == str(filters.owner_id))
    if filters.unassigned:
        query = query.filter(LeadTracking.owner_id.is_(None))
    if filters.reviewed is True:
        query = query.filter(LeadTracking.reviewed == True)
    elif filters.reviewed is False:
        query = query.filter(or_(LeadTracking.id.is_(None), LeadTracking.reviewed == False))
    if filters.country:
        query = query.filter(or_(
            literal_contains(Lead.company_country, filters.country),
            literal_contains(Lead.contact_country, filters.country),
        ))
    if filters.industry:
        query = query.filter(literal_contains(Lead.industry, filters.industry))
    if filters.min_score is not None:
        query = query.filter(FinalScore.total_score >= filters.min_score)
    query = query.filter(*reminder_predicate(filters.follow_up, now))
    cutoff = now - timedelta(days=EVIDENCE_FRESH_DAYS)
    if filters.evidence == "fresh":
        query = query.filter(Lead.evidence_checked_at >= cutoff, Lead.evidence_checked_at <= now)
    elif filters.evidence == "stale":
        query = query.filter(Lead.evidence_checked_at < cutoff)
    elif filters.evidence == "unknown":
        query = query.filter(or_(Lead.evidence_checked_at.is_(None), Lead.evidence_checked_at > now))
    return query


def filtered_query(db, filters: LeadFilters, now: Optional[datetime] = None, *, base=None):
    return apply_filters(base if base is not None else db.query(Lead), filters, now)
