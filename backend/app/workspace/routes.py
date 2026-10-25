"""Authenticated team workspace and database-backed lead segments/reminders."""

import logging
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, case, delete, func, or_, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload, load_only, noload

from app.database import get_db
from app.lead_results import LeadResultResponse, latest_refreshes, lead_to_response
from app.models import FinalScore, Job, Lead, LeadTracking, SavedSegment, TeamMember
from app.utils.lead_identity import transaction_lock
from app.workspace.filters import (
    LeadFilters, filtered_query, lead_filter_params, reminder_predicate, utc_now,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workspace", tags=["Sales workspace"])


def _clean_name(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value:
        raise ValueError("A name is required.")
    if len(value) > 100:
        raise ValueError("Names must contain at most 100 characters.")
    return value


def _clean_email(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    value = value.strip().lower()
    if len(value) > 255 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter a valid email address.")
    return value


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: Optional[str] = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: Optional[str]) -> Optional[str]:
        return _clean_email(value)


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    email: Optional[str] = Field(default=None, max_length=255)
    active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("A name cannot be null.")
        return _clean_name(value)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: Optional[str]) -> Optional[str]:
        return _clean_email(value)

    @field_validator("active")
    @classmethod
    def active_not_null(cls, value: Optional[bool]) -> bool:
        if value is None:
            raise ValueError("Active cannot be null.")
        return value


class TeamResponse(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class SegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    filters: LeadFilters = Field(default_factory=LeadFilters)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)


class SegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    filters: Optional[LeadFilters] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: Optional[str]) -> str:
        if value is None:
            raise ValueError("A name cannot be null.")
        return _clean_name(value)

    @field_validator("filters")
    @classmethod
    def filters_not_null(cls, value: Optional[LeadFilters]) -> LeadFilters:
        if value is None:
            raise ValueError("Filters cannot be null.")
        return value


class SegmentResponse(BaseModel):
    id: str
    name: str
    filters: LeadFilters
    version: int
    created_at: datetime
    updated_at: datetime


class WorkspaceLeadResponse(LeadResultResponse):
    job_id: str
    file_name: Optional[str] = None
    row_number: Optional[int] = None


class WorkspacePage(BaseModel):
    items: list[WorkspaceLeadResponse]
    total: int
    limit: int
    offset: int
    returned: int
    has_more: bool


class ReminderPage(WorkspacePage):
    due_count: int
    upcoming_count: int
    completed_count: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@contextmanager
def _database_errors(db: Session):
    try:
        yield
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Sales workspace database operation failed")
        raise HTTPException(
            status_code=503, detail="The workspace database is temporarily unavailable. Please try again.",
            headers={"Retry-After": "3"},
        ) from exc


def _lock(db: Session, resource: str) -> None:
    try:
        transaction_lock(db, resource)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Another workspace update is in progress. Please try again.",
            headers={"Retry-After": "2"},
        ) from exc


def _team_response(member: TeamMember) -> TeamResponse:
    return TeamResponse(
        id=str(member.id), name=member.name, email=member.email, active=bool(member.active),
        version=member.version, created_at=_utc(member.created_at), updated_at=_utc(member.updated_at or member.created_at),
    )


def _segment_filters(segment: SavedSegment) -> LeadFilters:
    try:
        return LeadFilters.model_validate_json(segment.filters_json)
    except ValidationError as exc:
        logger.error("Invalid stored filter rules for segment %s", segment.id)
        raise HTTPException(status_code=503, detail="This saved segment needs to be repaired. Edit its filters and save again.") from exc


def _segment_response(segment: SavedSegment) -> SegmentResponse:
    return SegmentResponse(
        id=str(segment.id), name=segment.name, filters=_segment_filters(segment),
        version=segment.version, created_at=_utc(segment.created_at), updated_at=_utc(segment.updated_at or segment.created_at),
    )


def _check_team_duplicate(db: Session, name: str, email: Optional[str], excluded_id: Optional[str] = None):
    matches = [func.lower(TeamMember.name) == name.lower()]
    if email:
        matches.append(func.lower(TeamMember.email) == email.lower())
    query = db.query(TeamMember.id).filter(or_(*matches))
    if excluded_id:
        query = query.filter(TeamMember.id != excluded_id)
    if query.first():
        raise HTTPException(status_code=409, detail="A team member with this name or email already exists. Update or reactivate that member.")


def _check_segment_duplicate(db: Session, name: str, excluded_id: Optional[str] = None):
    query = db.query(SavedSegment.id).filter(func.lower(SavedSegment.name) == name.lower())
    if excluded_id:
        query = query.filter(SavedSegment.id != excluded_id)
    if query.first():
        raise HTTPException(status_code=409, detail="A saved segment with this name already exists. Choose a different name or update it.")


@router.get("/team", response_model=list[TeamResponse])
def list_team(include_inactive: bool = Query(True), db: Session = Depends(get_db)):
    with _database_errors(db):
        query = db.query(TeamMember)
        if not include_inactive:
            query = query.filter(TeamMember.active == True)
        return [_team_response(member) for member in query.order_by(
            TeamMember.active.desc(), TeamMember.name.asc(), TeamMember.id.asc()).all()]


@router.post("/team", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team_member(body: TeamCreate, db: Session = Depends(get_db)):
    with _database_errors(db):
        _lock(db, "ql:workspace:team")
        _check_team_duplicate(db, body.name, body.email)
        now = utc_now()
        member = TeamMember(
            id=str(uuid4()), name=body.name, email=body.email, active=True,
            version=0, created_at=now, updated_at=now,
        )
        db.add(member)
        db.commit()
        return _team_response(member)


@router.patch("/team/{member_id}", response_model=TeamResponse)
def update_team_member(member_id: UUID, body: TeamUpdate, db: Session = Depends(get_db)):
    with _database_errors(db):
        changes = body.model_dump(exclude_unset=True, exclude={"version"})
        if not changes:
            raise HTTPException(status_code=422, detail="Provide a name, email, or active status to update.")
        _lock(db, "ql:workspace:team")
        member = db.get(TeamMember, str(member_id))
        if member is None:
            raise HTTPException(status_code=404, detail="Team member not found.")
        if member.version != body.version:
            raise HTTPException(status_code=409, detail="This team member changed elsewhere. Reload the workspace and try again.")
        _check_team_duplicate(db, changes.get("name", member.name), changes.get("email", member.email), str(member_id))
        changes.update(version=TeamMember.version + 1, updated_at=utc_now())
        result = db.execute(update(TeamMember).where(
            TeamMember.id == str(member_id), TeamMember.version == body.version,
        ).values(**changes).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="This team member changed elsewhere. Reload the workspace and try again.")
        db.commit()
        db.refresh(member)
        return _team_response(member)


@router.get("/segments", response_model=list[SegmentResponse])
def list_segments(db: Session = Depends(get_db)):
    with _database_errors(db):
        return [_segment_response(segment) for segment in db.query(SavedSegment).order_by(
            SavedSegment.name.asc(), SavedSegment.id.asc()).all()]


@router.post("/segments", response_model=SegmentResponse, status_code=status.HTTP_201_CREATED)
def create_segment(body: SegmentCreate, db: Session = Depends(get_db)):
    with _database_errors(db):
        _lock(db, "ql:workspace:segments")
        _check_segment_duplicate(db, body.name)
        now = utc_now()
        segment = SavedSegment(
            id=str(uuid4()), name=body.name, filters_json=body.filters.model_dump_json(),
            version=0, created_at=now, updated_at=now,
        )
        db.add(segment)
        db.commit()
        return _segment_response(segment)


@router.patch("/segments/{segment_id}", response_model=SegmentResponse)
def update_segment(segment_id: UUID, body: SegmentUpdate, db: Session = Depends(get_db)):
    with _database_errors(db):
        changes = {}
        if "name" in body.model_fields_set:
            changes["name"] = body.name
        if "filters" in body.model_fields_set:
            changes["filters_json"] = body.filters.model_dump_json()
        if not changes:
            raise HTTPException(status_code=422, detail="Provide a name or filters to update.")
        _lock(db, "ql:workspace:segments")
        segment = db.get(SavedSegment, str(segment_id))
        if segment is None:
            raise HTTPException(status_code=404, detail="Saved segment not found.")
        if segment.version != body.version:
            raise HTTPException(status_code=409, detail="This segment changed elsewhere. Reload the workspace and try again.")
        if "name" in changes:
            _check_segment_duplicate(db, changes["name"], str(segment_id))
        changes.update(version=SavedSegment.version + 1, updated_at=utc_now())
        result = db.execute(update(SavedSegment).where(
            SavedSegment.id == str(segment_id), SavedSegment.version == body.version,
        ).values(**changes).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="This segment changed elsewhere. Reload the workspace and try again.")
        db.commit()
        db.refresh(segment)
        return _segment_response(segment)


@router.delete("/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_segment(segment_id: UUID, version: int = Query(..., ge=0), db: Session = Depends(get_db)):
    with _database_errors(db):
        _lock(db, "ql:workspace:segments")
        if db.get(SavedSegment, str(segment_id)) is None:
            raise HTTPException(status_code=404, detail="Saved segment not found.")
        result = db.execute(delete(SavedSegment).where(
            SavedSegment.id == str(segment_id), SavedSegment.version == version,
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="This segment changed elsewhere. Reload the workspace and try again.")
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def resolve_filters(db: Session, segment_id: Optional[UUID], filters: LeadFilters, query_keys) -> LeadFilters:
    """A saved segment supplies rules; explicitly supplied query rules override it."""
    if segment_id is None:
        return filters
    segment = db.get(SavedSegment, str(segment_id))
    if segment is None:
        raise HTTPException(status_code=404, detail="Saved segment not found.")
    values = _segment_filters(segment).model_dump()
    for key in query_keys:
        if key in LeadFilters.model_fields:
            values[key] = getattr(filters, key)
    try:
        return LeadFilters.model_validate(values)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="The requested filters conflict with this saved segment.") from exc


def _representative_query(db: Session, filters: LeadFilters, now: datetime):
    """Return one latest researched copy per shared lead identity across uploads.

    A job-specific view chooses representatives inside that upload, so an older
    upload still opens correctly after the same lead is imported again.
    """
    copies = db.query(
        Lead.id.label("lead_id"),
        func.row_number().over(
            partition_by=func.coalesce(Lead.tracking_id, Lead.id),
            order_by=(
                case((Lead.evidence_checked_at.is_(None), 1), else_=0).asc(),
                Lead.evidence_checked_at.desc(), Lead.created_at.desc(), Job.created_at.desc(), Lead.id.asc(),
            ),
        ).label("copy_rank"),
    ).join(Job, Job.id == Lead.job_id)
    if filters.job_id is not None:
        copies = copies.filter(Lead.job_id == str(filters.job_id))
    copies = copies.subquery()
    base = db.query(Lead).join(copies, and_(copies.c.lead_id == Lead.id, copies.c.copy_rank == 1))
    return filtered_query(db, filters, now, base=base)


def _lead_item(lead: Lead, details=True, latest=None) -> WorkspaceLeadResponse:
    value = lead_to_response(lead, include_details=details, latest_refresh=latest).model_dump()
    value.update(
        job_id=str(lead.job_id), file_name=lead.job.file_name if lead.job else None,
        row_number=lead.row_index + 1 if lead.row_index is not None else None,
    )
    return WorkspaceLeadResponse(**value)


def _page(query, total: int, limit: int, offset: int, ordering, *, details=True) -> WorkspacePage:
    score_loader = joinedload(Lead.final_score)
    if not details:
        score_loader = score_loader.load_only(FinalScore.total_score, FinalScore.icp_score,
            FinalScore.growth_score, FinalScore.contact_score, FinalScore.priority,
            FinalScore.qualification_status, FinalScore.scoring_version)
    summary_load = [] if details else [load_only(Lead.id, Lead.job_id, Lead.row_index,
        Lead.contact_name, Lead.company_name, Lead.job_title, Lead.email_address,
        Lead.reuse_kind, Lead.reused_from_lead_id, Lead.evidence_checked_at, Lead.tracking_id)]
    leads = query.options(
        joinedload(Lead.job).load_only(Job.file_name, Job.icp_prompt), score_loader,
        selectinload(Lead.validation_results) if details else noload(Lead.validation_results),
        noload(Lead.refresh_requests), *summary_load,
    ).order_by(*ordering, Lead.id.asc()).offset(offset).limit(limit).all()
    latest = latest_refreshes(query.session, [lead.id for lead in leads])
    items = [_lead_item(lead, details, latest.get(str(lead.id))) for lead in leads]
    return WorkspacePage(
        items=items, total=total, limit=limit, offset=offset, returned=len(items),
        has_more=offset + len(items) < total,
    )


@router.get("/leads", response_model=WorkspacePage)
def workspace_leads(
    request: Request,
    details: bool = Query(True),
    filters: LeadFilters = Depends(lead_filter_params),
    segment_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: Literal["recent", "company_name", "contact_name", "final_score", "follow_up_at", "evidence_checked_at"] = Query("final_score"),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
    db: Session = Depends(get_db),
):
    with _database_errors(db):
        filters = resolve_filters(db, segment_id, filters, request.query_params.keys())
        if filters.job_id is not None and db.get(Job, str(filters.job_id)) is None:
            raise HTTPException(status_code=404, detail="Upload not found.")
        query = _representative_query(db, filters, utc_now())
        total = int(query.with_entities(func.count(Lead.id)).scalar() or 0)
        columns = {
            "recent": Lead.created_at, "company_name": Lead.company_name,
            "contact_name": Lead.contact_name, "final_score": FinalScore.total_score,
            "follow_up_at": LeadTracking.follow_up_at, "evidence_checked_at": Lead.evidence_checked_at,
        }
        column = columns[sort_by]
        order = column.asc() if sort_dir == "asc" else column.desc()
        return _page(query, total, limit, offset, (case((column.is_(None), 1), else_=0).asc(), order), details=details)


@router.get("/reminders", response_model=ReminderPage)
def workspace_reminders(
    details: bool = Query(True),
    status_filter: Literal["all", "due", "upcoming", "completed"] = Query("due", alias="status"),
    owner_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    with _database_errors(db):
        now = utc_now()
        base = _representative_query(db, LeadFilters(owner_id=owner_id), now)
        base = base.filter(or_(
            LeadTracking.follow_up_at.isnot(None), LeadTracking.follow_up_completed_at.isnot(None),
        ))
        states = ("due", "upcoming", "completed")
        totals = base.with_entities(*(
            func.sum(case((and_(*reminder_predicate(state, now)), 1), else_=0))
            for state in states
        )).one()
        counts = {state: int(totals[index] or 0) for index, state in enumerate(states)}
        query = base.filter(*reminder_predicate(status_filter, now))
        total = sum(counts.values()) if status_filter == "all" else counts[status_filter]
        if status_filter == "completed":
            ordering = (LeadTracking.follow_up_completed_at.desc(),)
        else:
            ordering = (
                case((LeadTracking.follow_up_completed_at.isnot(None), 1), else_=0).asc(),
                LeadTracking.follow_up_at.asc(),
            )
        page = _page(query, total, limit, offset, ordering, details=details)
        return ReminderPage(
            **page.model_dump(), due_count=counts["due"],
            upcoming_count=counts["upcoming"], completed_count=counts["completed"],
        )
