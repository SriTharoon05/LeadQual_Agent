"""Indexed, conservative identity matching and durable result reuse."""
import hashlib
import json
import unicodedata
import uuid
from urllib.parse import urlsplit

from sqlalchemy import exists, or_, text
from sqlalchemy.orm import selectinload

from app.models import FinalScore, Lead, LeadRefresh, LeadTracking, ValidationResult


def normalized(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "" if value in {"nan", "none", "null", "n/a", "unknown", "-"} else " ".join(value.split())


def words(value):
    return "".join(c for c in normalized(value) if c.isalnum())


def url_identity(value):
    value = normalized(value)
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").removeprefix("www.")
        if (host != "linkedin.com" and not host.endswith(".linkedin.com")) or not parsed.path.startswith(("/in/", "/pub/")):
            return ""
        return "linkedin.com" + parsed.path.rstrip("/")
    except ValueError:
        return ""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


KEY_FIELDS = ("crm_key", "email_key", "linkedin_key", "person_company_key")


def identity_fields(record):
    get = record.get if isinstance(record, dict) else lambda key: getattr(record, key, None)
    name, company = words(get("contact_name")), words(get("company_name"))
    return {
        "crm_key": digest(normalized(get("crm_contact_id"))),
        "email_key": digest(normalized(get("email_address"))),
        "linkedin_key": digest(url_identity(get("linkedin_profile_url"))),
        "person_company_key": digest(name + "|" + company) if name and company else None,
    }


def fingerprints(record):
    get = record.get if isinstance(record, dict) else lambda key: getattr(record, key, None)
    research = {key: normalized(get(key)) for key in (
        "contact_name", "company_name", "company_website", "linkedin_profile_url", "job_title")}
    inputs = {key: normalized(get(key)) for key in (
        "contact_department", "contact_seniority", "industry", "sub_industry", "employee_count",
        "annual_revenue", "company_country", "company_state_region", "company_city",
        "company_hq_location", "email_address", "phone_number", "direct_phone_number", "contact_country")}
    return {
        "research_fingerprint": digest(json.dumps(research, sort_keys=True)),
        "input_fingerprint": digest(json.dumps({**research, **inputs}, sort_keys=True)),
    }


def transaction_lock(db, resource):
    """Serialize short identity/import transactions across API and workers."""
    if db.bind.dialect.name == "mssql":
        code = db.execute(text("""
            SET NOCOUNT ON;
            DECLARE @result int;
            EXEC @result = sp_getapplock @Resource=:resource, @LockMode='Exclusive',
                 @LockOwner='Transaction', @LockTimeout=30000;
            SELECT @result;
        """), {"resource": resource}).scalar()
        if code is None or code < 0:
            raise RuntimeError("Could not acquire database import lock")


def matching_lead(db, keys):
    predicates = [getattr(Lead, key) == value for key, value in keys.items() if value]
    if not predicates:
        return None
    candidates = db.query(Lead).filter(or_(*predicates)).order_by(Lead.created_at.desc(), Lead.id).all()
    # Conflicting strong identifiers are ambiguous: never merge them silently.
    valid = [lead for lead in candidates if not any(
        keys[key] and getattr(lead, key) and keys[key] != getattr(lead, key)
        for key in KEY_FIELDS[:3])]
    groups = {str(lead.tracking_id) for lead in valid if lead.tracking_id}
    return valid[0] if valid and len(groups) <= 1 else None


def assign_identity(db, record):
    keys = identity_fields(record)
    previous = matching_lead(db, keys)
    tracking_id = previous.tracking_id if previous else None
    if not tracking_id:
        tracking_id = str(uuid.uuid4())
        db.add(LeadTracking(id=tracking_id, reviewed=False, version=0))
        db.flush()
    record.update(keys, **fingerprints(record), tracking_id=tracking_id)


def assign_identities(db, records):
    """Batch indexed lookups, bounded below SQL Server's 2,100-parameter limit.

    Caller holds the import transaction lock until all lead rows are inserted.
    In-memory aliases include earlier rows from this upload without extra queries.
    """
    aliases = {}
    for start in range(0, len(records), 300):
        batch = records[start:start + 300]
        keys_by_row = [identity_fields(record) for record in batch]
        predicates = []
        for field in KEY_FIELDS:
            values = {keys[field] for keys in keys_by_row if keys[field]}
            if values:
                predicates.append(getattr(Lead, field).in_(values))
        if predicates:
            existing = (db.query(Lead).filter(or_(*predicates))
                        .order_by(Lead.created_at.desc(), Lead.id).all())
            for lead in existing:
                candidate = {field: getattr(lead, field) for field in KEY_FIELDS}
                candidate["tracking_id"] = lead.tracking_id
                for field in KEY_FIELDS:
                    if candidate[field]:
                        aliases.setdefault((field, candidate[field]), {})[str(lead.id)] = candidate
        for record, keys in zip(batch, keys_by_row):
            candidates = {}
            for field in KEY_FIELDS:
                if keys[field]:
                    candidates.update(aliases.get((field, keys[field]), {}))
            groups = {str(candidate["tracking_id"]) for candidate in candidates.values()
                      if candidate["tracking_id"] and not any(
                          keys[field] and candidate[field] and keys[field] != candidate[field]
                          for field in KEY_FIELDS[:3])}
            tracking_id = next(iter(groups)) if len(groups) == 1 else str(uuid.uuid4())
            if len(groups) != 1:
                db.add(LeadTracking(id=tracking_id, reviewed=False, version=0))
            record.update(keys, **fingerprints(record), tracking_id=tracking_id)
            candidate = {**keys, "tracking_id": tracking_id}
            for field in KEY_FIELDS:
                if keys[field]:
                    aliases.setdefault((field, keys[field]), {})[record["id"]] = candidate
    db.flush()


def reusable_lead(db, lead, icp_prompt):
    """Only finished results in the same person/company/role context qualify."""
    if not lead.tracking_id or not lead.research_fingerprint:
        return None, None
    candidates = (db.query(Lead).filter(
        Lead.tracking_id == lead.tracking_id, Lead.id != lead.id,
        or_(Lead.processing_status == "completed", exists().where(
            LeadRefresh.lead_id == Lead.id, LeadRefresh.status == "completed")),
        ~exists().where(LeadRefresh.lead_id == Lead.id,
                        LeadRefresh.status.in_(("queued", "running"))),
        Lead.research_fingerprint == lead.research_fingerprint,
    ).options(selectinload(Lead.validation_results), selectinload(Lead.final_score),
              selectinload(Lead.job)).order_by(
                  Lead.evidence_checked_at.desc(), Lead.created_at.desc(), Lead.id).all())
    for candidate in candidates:
        stages = {v.stage: v for v in candidate.validation_results}
        if not candidate.final_score or not {"icp", "web", "contact"}.issubset(stages):
            continue
        # Failures masquerading as safe defaults must not become reusable evidence.
        if any(json_object(v.raw_output).get("error") for v in stages.values()):
            continue
        if (candidate.input_fingerprint == lead.input_fingerprint
                and normalized(candidate.job.icp_prompt) == normalized(icp_prompt)):
            return candidate, "full"
        # Legacy research without structured evidence cannot validate a new ICP.
        evidence = json_object(stages["web"].raw_output).get("evidence")
        if isinstance(evidence, dict) and evidence:
            # Fresh research takes precedence over an older score for a matching
            # ICP: score the new evidence instead of restoring outdated facts.
            return candidate, "research"
    return None, None


def json_object(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def copy_result(db, source, target):
    """Independent snapshots preserve new jobs when an older job is deleted."""
    for value in source.validation_results:
        db.add(ValidationResult(lead_id=target.id, **{
            name: getattr(value, name) for name in (
                "stage", "passed", "score", "reasoning", "raw_output", "sources_json", "created_at")}))
    db.add(FinalScore(lead_id=target.id, **{
        column.name: getattr(source.final_score, column.name)
        for column in FinalScore.__table__.columns if column.name not in {"id", "lead_id"}}))
    target.company_description = source.company_description
    target.technologies_used = source.technologies_used
    target.evidence_checked_at = source.evidence_checked_at
    target.reused_from_lead_id = source.reused_from_lead_id or source.id
    target.reuse_kind = "full"
