"""Scoped SQLAlchemy retrieval and bounded, source-attributed chat records."""

import json
from datetime import datetime, timezone

from sqlalchemy import UnicodeText, case, cast, func, literal_column, or_, and_
from sqlalchemy.orm import selectinload

from app.models import FinalScore, Job, Lead, LeadTracking
from app.lead_results import follow_up_status, freshness, utc
from app.utils.lead_identity import json_object
from app.validators.evidence import clean_sources, proof_rows, safe_url
from app.chat.planning import LEAD_FIELDS, SCORE_FIELDS, QueryPlan


def _numeric_text(column):
    """Convert only a single unambiguous number; ranges remain unknown.

    All expressions/types below are fixed server code, never model-provided SQL.
    Revenue values keep their imported currency; no currency conversion occurs.
    """
    text = func.lower(func.ltrim(func.rtrim(column)))
    for character in (",", " ", "$", "€", "£"):
        text = func.replace(text, character, "")
    as_number = func.try_convert(literal_column("FLOAT"), text)
    prefix = func.left(text, func.nullif(func.len(text), 0) - 1)
    scaled = func.try_convert(literal_column("FLOAT"), prefix)
    return case((text.like("%k"), scaled * 1000), (text.like("%m"), scaled * 1000000),
                (text.like("%b"), scaled * 1000000000), else_=as_number)


def column_for(name: str, numeric: bool = False):
    if name == "row_number":
        return Lead.row_index + 1
    if name == "reviewed":
        return func.coalesce(LeadTracking.reviewed, False)
    if name == "tracking_notes":
        return cast(LeadTracking.notes, UnicodeText)
    if name in SCORE_FIELDS:
        column = getattr(FinalScore, name)
        return cast(column, UnicodeText) if name in {"reasoning", "growth_signals", "outreach_intelligence"} else column
    if name in LEAD_FIELDS:
        column = getattr(Lead, name)
        if name in {"notes", "company_description", "technologies_used"}:
            column = cast(column, UnicodeText)
        return _numeric_text(column) if numeric and name in {"employee_count", "annual_revenue"} else column
    raise ValueError("Unsupported chat field")


def _missing(column):
    return or_(column.is_(None), func.ltrim(func.rtrim(column)) == "")


def _predicate(item: dict):
    name, operator, value = item["field"], item["op"], item.get("value")
    column = column_for(name, numeric=operator in {"gt", "gte", "lt", "lte"}
                        or isinstance(value, (int, float)) and not isinstance(value, bool))
    if operator in {"is_missing", "is_present"}:
        if name == "phone_number":
            missing = and_(_missing(Lead.phone_number), _missing(Lead.direct_phone_number))
        elif name == "tracking_notes":
            missing = and_(_missing(cast(LeadTracking.notes, UnicodeText)), _missing(cast(Lead.notes, UnicodeText)))
        elif name in {"total_score", "icp_score", "authority_score", "data_score", "growth_score", "contact_score", "row_number", "reviewed"}:
            missing = column.is_(None)
        else:
            missing = _missing(column)
        return missing if operator == "is_missing" else ~missing
    if operator in {"eq", "ne"}:
        predicate = func.lower(column) == value.lower() if isinstance(value, str) else column == value
        return predicate if operator == "eq" else ~predicate
    if operator == "contains":
        needle = value.casefold()
        if name == "phone_number":
            return or_(func.lower(Lead.phone_number).contains(needle, autoescape=True),
                       func.lower(Lead.direct_phone_number).contains(needle, autoescape=True))
        return func.lower(column).contains(needle, autoescape=True)
    return {"gt": column.__gt__, "gte": column.__ge__, "lt": column.__lt__, "lte": column.__le__}[operator](value)


def _bounded(value, limit=1400):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= limit else text[:limit] + " [stored text continues]"


def _record(lead: Lead, icp_input: str | None) -> dict:
    score, tracking = lead.final_score, lead.tracking
    result = {name: _bounded(getattr(lead, name)) for name in LEAD_FIELDS}
    result.update({name: _bounded(getattr(score, name)) if score else None for name in SCORE_FIELDS})
    result.update({"lead_id": str(lead.id),
                   "row_number": lead.row_index + 1 if lead.row_index is not None else None,
                   "reviewed": bool(tracking and tracking.reviewed),
                   "tracking_notes": _bounded(tracking.notes) if tracking else None,
                   "icp_input": _bounded(icp_input, 3000)})
    now = datetime.now(timezone.utc)
    owner = tracking.owner if tracking else None
    evidence_status, evidence_age = freshness(lead.evidence_checked_at, now)
    runs = lead.refresh_requests or []
    active_refresh = next((run for run in runs if run.status in {"queued", "running"}), None)
    latest_refresh = active_refresh or next(iter(runs), None)
    result.update({
        "owner_name": _bounded(owner.name) if owner else "Unassigned",
        "owner_active": bool(owner.active) if owner else None,
        "follow_up_at": _date(tracking.follow_up_at) if tracking else None,
        "follow_up_note": _bounded(tracking.follow_up_note) if tracking else None,
        "follow_up_completed_at": _date(tracking.follow_up_completed_at) if tracking else None,
        "follow_up_status": follow_up_status(tracking, now),
        "evidence_checked_at": _date(lead.evidence_checked_at),
        "evidence_age_days": evidence_age, "evidence_status": evidence_status,
        "refresh_status": latest_refresh.status if latest_refresh else "Not requested",
    })
    stages = {validation.stage: json_object(validation.raw_output) for validation in lead.validation_results}
    result["icp_validation_proofs"] = proof_rows(stages.get("web", {}), stages.get("icp", {}))
    for proof in result["icp_validation_proofs"]:
        for name in ("requirement", "reasoning", "explanation", "value", "label", "checked_at"):
            proof[name] = _bounded(proof.get(name), 700)
        proof["sources"] = [{"url": source["url"], "title": _bounded(source.get("title"), 160),
                              "excerpt": _bounded(source.get("excerpt"), 350)}
                             for source in proof.get("sources", [])[:3]]
    # Include saved stage verdicts/verification dates, never private prompts or arbitrary raw output.
    result["validation_results"] = [{"stage": validation.stage, "passed": validation.passed,
                                      "score": validation.score, "reasoning": _bounded(validation.reasoning),
                                      "checked_at": str(validation.created_at) if validation.created_at else None}
                                     for validation in lead.validation_results]
    contact = stages.get("contact", {})
    if contact:
        result["contact_verification"] = {
            name: _bounded(contact.get(name), 600)
            for name in ("email_valid", "email_verified", "phone_valid", "linkedin_valid", "reasoning")
            if name in contact
        }
    scrape = lead.company_scrape
    if scrape and scrape.clean_text:
        result["stored_website_context"] = {"text": _bounded(scrape.clean_text, 1800),
                                            "website_url": safe_url(scrape.website_url),
                                            "checked_at": str(scrape.scraped_at) if scrape.scraped_at else None}
    sources = []
    for proof in result["icp_validation_proofs"]:
        for source in proof.get("sources", []):
            sources.append({"company_name": lead.company_name, "website_url": source["url"],
                            "title": source.get("title"), "excerpt": source.get("excerpt")})
    for validation in lead.validation_results:
        raw_sources = stages.get(validation.stage, {}).get("sources", [])
        try:
            saved_sources = json.loads(validation.sources_json or "[]")
        except (TypeError, ValueError):
            saved_sources = []
        for url in clean_sources(raw_sources) + clean_sources(saved_sources):
            sources.append({"company_name": lead.company_name, "website_url": url})
    if scrape and safe_url(scrape.website_url):
        sources.append({"company_name": lead.company_name, "website_url": safe_url(scrape.website_url),
                        "title": "Stored company website"})
    result["_sources"] = sources
    return result


def _date(value):
    return utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def retrieve(db, job_id: str, plan: QueryPlan, scope: dict) -> dict:
    """Every count/filter/order is evaluated over the entire job before paging."""
    query = (db.query(Lead).outerjoin(FinalScore, FinalScore.lead_id == Lead.id)
             .outerjoin(LeadTracking, LeadTracking.id == Lead.tracking_id)
             .filter(Lead.job_id == job_id))
    if scope.get("companies"):
        query = query.filter(Lead.company_name.in_(scope["companies"]))
    if scope.get("person_ids"):
        query = query.filter(Lead.id.in_(scope["person_ids"]))
    if plan.row_numbers:
        query = query.filter(Lead.row_index.in_([number - 1 for number in plan.row_numbers]))
    for item in plan.filters:
        query = query.filter(_predicate(item))
    query = query.enable_eagerloads(False)
    total = query.with_entities(func.count(Lead.id)).scalar() or 0
    context = {"matched_count_total": total, "offset": plan.offset, "limit": plan.limit,
               "matched_contacts": [], "companies": [], "sources": [],
               "query": plan.to_dict(), "scope": {key: value for key, value in scope.items() if value},
               "numeric_filter_note": any(item["field"] in {"employee_count", "annual_revenue"}
                                          and isinstance(item.get("value"), (int, float)) for item in plan.filters)}
    if plan.action == "count":
        return context
    if plan.action == "companies":
        ranking_metric = plan.sort_by if plan.sort_by in SCORE_FIELDS else "total_score"
        ranking_average = func.avg(column_for(ranking_metric))
        grouped = (query.filter(Lead.company_name.isnot(None), func.ltrim(func.rtrim(Lead.company_name)) != "")
                   .with_entities(Lead.company_name.label("company_name"), func.count(Lead.id).label("lead_count"),
                                  func.avg(FinalScore.total_score).label("average_score"),
                                  func.max(FinalScore.total_score).label("highest_score"),
                                  ranking_average.label("ranking_score"))
                   .group_by(Lead.company_name))
        context["matched_count_companies"] = grouped.count()
        order = func.avg(column_for(plan.sort_by)) if plan.sort_by in SCORE_FIELDS else Lead.company_name
        grouped = grouped.order_by(case((order.is_(None), 1), else_=0),
                                   order.desc() if plan.direction == "desc" else order.asc(), Lead.company_name.asc())
        context["companies"] = [{"company_name": name, "lead_count": count,
                                  "average_score": round(float(average), 2) if average is not None else None,
                                  "highest_score": highest, "ranking_metric": ranking_metric,
                                  "ranking_score": round(float(ranking), 2) if ranking is not None else None}
                                 for name, count, average, highest, ranking in grouped.offset(plan.offset).limit(plan.limit).all()]
        context["has_more"] = plan.offset + len(context["companies"]) < context["matched_count_companies"]
        return context
    if plan.action == "summary":
        context["summary"] = {
            "priorities": {priority or "Unscored": count for priority, count in
                           query.with_entities(FinalScore.priority, func.count(Lead.id)).group_by(FinalScore.priority).all()},
            "processing": {status or "Unknown": count for status, count in
                           query.with_entities(Lead.processing_status, func.count(Lead.id)).group_by(Lead.processing_status).all()},
            "reviewed": query.filter(LeadTracking.reviewed == 1).with_entities(func.count(Lead.id)).scalar() or 0,
            "average_score": query.with_entities(func.avg(FinalScore.total_score)).scalar(),
            "unique_companies": query.filter(Lead.company_name.isnot(None), func.ltrim(func.rtrim(Lead.company_name)) != "").with_entities(func.count(func.distinct(Lead.company_name))).scalar() or 0,
        }
        return context
    order = column_for(plan.sort_by, numeric=plan.sort_by in {"employee_count", "annual_revenue"})
    leads = (query.enable_eagerloads(True)
             .options(selectinload(Lead.final_score), selectinload(Lead.validation_results),
                      selectinload(Lead.company_scrape), selectinload(Lead.tracking),
                      selectinload(Lead.refresh_requests))
             .order_by(case((order.is_(None), 1), else_=0), order.desc() if plan.direction == "desc" else order.asc(),
                       Lead.row_index.asc(), Lead.id.asc())
             .offset(plan.offset).limit(plan.limit).all())
    icp_input = db.query(Job.icp_prompt).filter(Job.id == job_id).scalar()
    rows = [_record(lead, icp_input) for lead in leads]
    sources = {}
    for row in rows:
        for source in row.pop("_sources", []):
            sources.setdefault(source["website_url"], source)
    context["sources"] = list(sources.values())[:30]
    context["matched_contacts"] = rows
    context["has_more"] = plan.offset + len(rows) < total
    return context
