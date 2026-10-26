"""Validated job-result filters; no assignment or reminder surface."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from app.models import FinalScore, Lead, LeadTracking


class LeadFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=100)
    priority: Literal["High", "Medium", "Low"] | None = None
    reviewed: bool | None = None
    country: str = Field(default="", max_length=100)
    industry: str = Field(default="", max_length=100)
    min_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    evidence: Literal["all", "fresh", "stale", "unknown"] = "all"

    @field_validator("search", "country", "industry")
    @classmethod
    def clean_text(cls, value):
        return " ".join(value.split())


def literal_contains(column, value):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
    return column.ilike("%" + escaped + "%", escape="\\")


def filtered_query(db, filters, now=None, *, base=None):
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    query = (base if base is not None else db.query(Lead)).outerjoin(
        FinalScore, FinalScore.lead_id == Lead.id).outerjoin(LeadTracking, LeadTracking.id == Lead.tracking_id)
    if filters.search:
        query = query.filter(or_(*(literal_contains(column, filters.search) for column in
            (Lead.company_name, Lead.contact_name, Lead.email_address, Lead.job_title))))
    if filters.priority is not None:
        query = query.filter(FinalScore.priority == filters.priority)
    if filters.reviewed is True:
        query = query.filter(LeadTracking.reviewed == True)
    elif filters.reviewed is False:
        query = query.filter(or_(LeadTracking.id.is_(None), LeadTracking.reviewed == False))
    if filters.country:
        query = query.filter(or_(literal_contains(Lead.company_country, filters.country), literal_contains(Lead.contact_country, filters.country)))
    if filters.industry:
        query = query.filter(literal_contains(Lead.industry, filters.industry))
    if filters.min_score is not None:
        query = query.filter(FinalScore.total_score >= filters.min_score)
    cutoff = now - timedelta(days=90)
    if filters.evidence == "fresh":
        query = query.filter(Lead.evidence_checked_at >= cutoff, Lead.evidence_checked_at <= now)
    elif filters.evidence == "stale":
        query = query.filter(Lead.evidence_checked_at < cutoff)
    elif filters.evidence == "unknown":
        query = query.filter(or_(Lead.evidence_checked_at.is_(None), Lead.evidence_checked_at > now))
    return query
