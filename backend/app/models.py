"""
models.py
─────────
SQLAlchemy ORM models — one class per Azure SQL table.
Mirrors the schema exactly. Import Base from database.py.

Import pattern everywhere else:
    from app.models import Job, Lead, ValidationResult, FinalScore, ProcessingLog
"""

import uuid
from decimal import Decimal
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Numeric,
    Index, Integer, String, Text, Unicode, UnicodeText, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.mssql import UNIQUEIDENTIFIER
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

def _new_uuid() -> str:
    return str(uuid.uuid4())


# ── jobs ──────────────────────────────────────────────────────────────────────
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_dispatch_recovery", "status", "dispatch_complete", "dispatch_updated_at"),
        CheckConstraint("processing_mode IN ('reuse','fresh')", name="ck_jobs_processing_mode"),
    )

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="queued")
    # queued | running | completed | failed

    total_rows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reused_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    research_reused_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    icp_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="reuse", server_default=text("'reuse'"))
    file_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    file_type: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    duplicates_removed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    import_error:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_blob_name: Mapped[Optional[str]] = mapped_column(Unicode(1000), nullable=True)
    upload_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dispatch_row_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dispatch_lead_id: Mapped[Optional[str]] = mapped_column(UNIQUEIDENTIFIER, nullable=True)
    dispatch_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    dispatch_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dispatch_epoch: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # xlsx | csv

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Add to Job model (inside the class, after existing mapped_column lines) ──
    llm_model:      Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    embed_model:    Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    total_tokens_used: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    prompt_tokens:  Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Relationships
    leads: Mapped[list["Lead"]] = relationship(
        "Lead", back_populates="job", cascade="all, delete-orphan"
    )
    logs: Mapped[list["ProcessingLog"]] = relationship(
        "ProcessingLog", back_populates="job", cascade="all, delete-orphan",
        foreign_keys="ProcessingLog.job_id",
    )

    llm_call_logs: Mapped[list["LLMCallLog"]] = relationship(
        "LLMCallLog", cascade="all, delete-orphan",
        foreign_keys="LLMCallLog.job_id",
    )    

    chat_conversations: Mapped[list["ChatConversation"]] = relationship(
        "ChatConversation", cascade="all, delete-orphan")

    @property
    def progress_pct(self) -> float:
        if not self.total_rows:
            return 0.0
        return round((self.processed_rows / self.total_rows) * 100, 1)

    def __repr__(self) -> str:
        return f"<Job id={self.id} status={self.status} rows={self.processed_rows}/{self.total_rows}>"


# ── llm_call_logs ─────────────────────────────────────────────────────────────
class LLMCallLog(Base):
    __tablename__ = "llm_call_logs"
    __table_args__ = (Index("ix_usage_job_lead_stage", "job_id", "lead_id", "stage"),)

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    job_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=True
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=True
    )
    stage: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    module: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    use_web_search: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    web_search_calls: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    billing_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cached_prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    search_requests: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    cost_input_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 10), nullable=True)
    cost_cached_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 10), nullable=True)
    cost_output_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 10), nullable=True)
    cost_search_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 10), nullable=True)
    cost_total_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 10), nullable=True)
    cost_complete: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    pricing_snapshot_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True, deferred=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    def __repr__(self) -> str:
        return (f"<LLMCallLog stage={self.stage} tokens={self.total_tokens} "
                f"job={self.job_id}>")


class RuntimeSettings(Base):
    __tablename__ = "runtime_settings"
    __table_args__ = (CheckConstraint("concurrency >= 1 AND concurrency <= 30", name="ck_runtime_concurrency"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    pricing_json: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    usd_inr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fx_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    fx_source: Mapped[Optional[str]] = mapped_column(Unicode(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(Unicode(100), nullable=True)


class OperationalEvent(Base):
    __tablename__ = "operational_events"
    __table_args__ = (
        Index("ix_events_job_id", "job_id", "id"),
        CheckConstraint("severity IN ('WARNING','ERROR')", name="ck_events_severity"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    # No FK: retain diagnostics when the associated upload is deleted.
    job_id: Mapped[Optional[str]] = mapped_column(UNIQUEIDENTIFIER, nullable=True)
    lead_id: Mapped[Optional[str]] = mapped_column(UNIQUEIDENTIFIER, nullable=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    component: Mapped[Optional[str]] = mapped_column(Unicode(100), nullable=True)
    message: Mapped[str] = mapped_column(Unicode(2000), nullable=False)
    retry_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.getutcdate())

# ── leads ─────────────────────────────────────────────────────────────────────
class TeamMember(Base):
    __tablename__ = "team_member"
    id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(Unicode(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.getutcdate())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SavedSegment(Base):
    __tablename__ = "saved_segment"
    id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    filters_json: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.getutcdate())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class LeadTracking(Base):
    """Shared manual tracking, independent of a particular upload/job."""
    __tablename__ = "lead_tracking"
    id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("team_member.id"), nullable=True, index=True)
    owner: Mapped[Optional["TeamMember"]] = relationship(lazy="noload")  # Legacy schema only.
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    follow_up_note: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)
    follow_up_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (Index("ix_tracking_owner_due", "owner_id", "follow_up_completed_at", "follow_up_at"),)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_job_row", "job_id", "row_index"),)

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    job_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=False
    )
    row_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tracking_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("lead_tracking.id"), nullable=True, index=True)
    crm_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    email_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    linkedin_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    person_company_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    input_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    research_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reused_from_lead_id: Mapped[Optional[str]] = mapped_column(UNIQUEIDENTIFIER, nullable=True)
    reuse_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tracking: Mapped[Optional["LeadTracking"]] = relationship(lazy="joined")

    # ── Contact Information (cols 1–8) ────────────────────────────────────────
    contact_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_department: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_seniority: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    linkedin_profile_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    email_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    direct_phone_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # ── Contact Location (cols 9–11) ──────────────────────────────────────────
    contact_country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    contact_state_region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    contact_city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # ── Company Information (cols 12–20) ──────────────────────────────────────
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    company_website: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    company_linkedin_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    sub_industry: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    employee_count: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    annual_revenue: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    company_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # AI FILL
    technologies_used: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # AI FILL

    # ── Company Location (cols 21–25) ─────────────────────────────────────────
    company_country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    company_state_region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    company_city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    company_hq_location: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    hq_phone_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # ── Additional Information (cols 26–29) ───────────────────────────────────
    crm_account_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    crm_contact_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lead_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    evidence_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="leads")
    validation_results: Mapped[list["ValidationResult"]] = relationship(
        "ValidationResult", back_populates="lead", cascade="all, delete-orphan"
    )
    final_score: Mapped[Optional["FinalScore"]] = relationship(
        "FinalScore", back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    company_scrape: Mapped[Optional["CompanyScrape"]] = relationship(
        "CompanyScrape", back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    logs: Mapped[list["ProcessingLog"]] = relationship(
        "ProcessingLog", back_populates="lead", cascade="all, delete-orphan",
        foreign_keys="ProcessingLog.lead_id",
    )
    llm_call_logs: Mapped[list["LLMCallLog"]] = relationship(
        "LLMCallLog", cascade="all, delete-orphan",
        foreign_keys="LLMCallLog.lead_id",
    )
    refresh_requests: Mapped[list["LeadRefresh"]] = relationship(
        "LeadRefresh", cascade="all, delete-orphan", lazy="selectin",
        order_by="desc(LeadRefresh.created_at), desc(LeadRefresh.id)")

    def __repr__(self) -> str:
        return f"<Lead id={self.id} name={self.contact_name} company={self.company_name}>"


class LeadRefresh(Base):
    __tablename__ = "lead_refresh"
    __table_args__ = (
        Index("ix_refresh_dispatch", "status", "last_dispatched_at", "created_at"),
        Index("uq_lead_refresh_active", "lead_id", unique=True,
              mssql_where=text("status IN ('queued', 'running')"),
              sqlite_where=text("status IN ('queued', 'running')")),
    )
    id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid)
    lead_id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    requested_by: Mapped[Optional[str]] = mapped_column(Unicode(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.getutcdate())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Unicode(1000), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    previous_snapshot_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True, deferred=True)


# ── validation_results ────────────────────────────────────────────────────────
class ValidationResult(Base):
    __tablename__ = "validation_results"
    __table_args__ = (UniqueConstraint("lead_id", "stage", name="uq_validation_lead_stage"),)

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    lead_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    # icp | web | contact

    passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # full JSON
    sources_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True) # URL array as JSON

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    # Relationships
    lead: Mapped["Lead"] = relationship("Lead", back_populates="validation_results")

    def __repr__(self) -> str:
        return f"<ValidationResult lead={self.lead_id} stage={self.stage} passed={self.passed}>"


# ── final_scores ──────────────────────────────────────────────────────────────
class FinalScore(Base):
    __tablename__ = "final_scores"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_final_score_lead"),)

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    lead_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=False
    )

    total_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    icp_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    authority_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    data_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    growth_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    contact_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scoring_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    qualification_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    scoring_details_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)
    scoring_audit_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True, deferred=True)

    priority: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # High | Medium | Low

    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    growth_signals: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outreach_intelligence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    # Relationships
    lead: Mapped["Lead"] = relationship("Lead", back_populates="final_score")

    def __repr__(self) -> str:
        return f"<FinalScore lead={self.lead_id} total={self.total_score} priority={self.priority}>"


# ── company_scrape (Phase 2 — schema only, not used yet) ─────────────────────
# ── company_scrape (RAG ingestion) ────────────────────────────────────────────
class CompanyScrape(Base):
    __tablename__ = "company_scrape"
    __table_args__ = (
        Index("ix_scrape_latest", "lead_id", "scraped_at", "id"),
        Index("ix_scrape_ingestion", "ingest_status", "ingest_dispatched_at"),
    )

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    job_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=False, index=True
    )
    lead_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=False, index=True
    )

    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website_url:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    clean_text:   Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)
    blob_path:    Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    chunk_count:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded:     Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0=pending 1=done
    ingest_status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default=text("'queued'"))
    ingest_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    ingest_dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ingest_error: Mapped[Optional[str]] = mapped_column(Unicode(1000), nullable=True)

    scraped_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    # Relationships
    lead: Mapped["Lead"] = relationship("Lead", back_populates="company_scrape")

    def __repr__(self) -> str:
        return f"<CompanyScrape lead={self.lead_id} url={self.website_url} embedded={self.embedded}>"


# ── admin_users ───────────────────────────────────────────────────────────────
class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        return f"<AdminUser username={self.username}>"


# ── chat_message (session-scoped chatbot history) ─────────────────────────────
class ChatConversation(Base):
    """One independent chat per browser conversation, scoped to its upload."""
    __tablename__ = "chat_conversation"

    id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid)
    job_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=False, index=True)
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate())
    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", cascade="all, delete-orphan", order_by="ChatMessage.sequence")


class ChatMessage(Base):
    __tablename__ = "chat_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_chat_conversation_sequence"),
        UniqueConstraint("conversation_id", "request_id", "role", name="uq_chat_request_role"),
    )

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    job_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("chat_conversation.id"), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(UNIQUEIDENTIFIER, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_used: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sources_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(UnicodeText, nullable=True)
    role:    Mapped[str] = mapped_column(String(20), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(UnicodeText, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    def __repr__(self) -> str:
        return f"<ChatMessage job={self.job_id} role={self.role}>"




# ── processing_logs ───────────────────────────────────────────────────────────
class ProcessingLog(Base):
    __tablename__ = "processing_logs"

    id: Mapped[str] = mapped_column(
        UNIQUEIDENTIFIER, primary_key=True, default=_new_uuid
    )
    job_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("jobs.id"), nullable=True
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        UNIQUEIDENTIFIER, ForeignKey("leads.id"), nullable=True
    )

    level: Mapped[str] = mapped_column(String(20), nullable=False, default="INFO")
    # INFO | WARNING | ERROR
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.getutcdate()
    )

    # Relationships
    job: Mapped[Optional["Job"]] = relationship(
        "Job", back_populates="logs", foreign_keys=[job_id]
    )
    lead: Mapped[Optional["Lead"]] = relationship(
        "Lead", back_populates="logs", foreign_keys=[lead_id]
    )

    def __repr__(self) -> str:
        return f"<ProcessingLog [{self.level}] job={self.job_id} lead={self.lead_id}>"
