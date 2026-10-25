"""Scoped SQLAlchemy retrieval and bounded, source-attributed chat records."""

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Date, Float, Integer, UnicodeText, case, cast, func, literal_column, or_, and_, select
from sqlalchemy.orm import aliased, joinedload, selectinload, noload, load_only

from app.models import CompanyScrape, FinalScore, Job, Lead, LeadRefresh, LeadTracking, TeamMember, ValidationResult
from app.lead_results import follow_up_status, freshness, utc
from app.utils.lead_identity import json_object
from app.validators.evidence import citation_key, clean_sources, proof_rows, safe_url
from app.validators.contact_policy import saved_contact
from app.chat.planning import CURRENCY_CODES, DATE_FIELDS, LEAD_FIELDS, SCORE_FIELDS, QueryPlan
from app.chat.sql_evidence import confirmed_fact_value


def _numeric_text(field_name):
    """Convert only a single unambiguous number; ranges remain unknown.

    All expressions/types below are fixed server code, never model-provided SQL.
    Revenue values keep their imported currency; no currency conversion occurs.
    """
    if field_name not in {"employee_count", "annual_revenue"}:
        raise ValueError("Unsupported numeric text field")
    # The derived FROM is deliberately uncorrelated: SQL Server does not allow
    # ordinary derived tables to reference an outer Lead alias without APPLY.
    # Resolve sourced values against this inner Lead, then correlate only the
    # enclosing scalar WHERE by its projected primary key. The large guarded
    # JSON expression is projected once rather than repeated in every branch.
    numeric_lead = aliased(Lead, name="numeric_lead")
    column = func.coalesce(confirmed_fact_value(field_name, lead=numeric_lead), getattr(numeric_lead, field_name))
    projected = select(numeric_lead.id.label("lead_id"), column.label("raw_value")).select_from(
        numeric_lead).correlate(None).subquery("numeric_input")
    text = func.lower(func.ltrim(func.rtrim(projected.c.raw_value)))
    for character in (",", " ", "$", "€", "£", "₹", "¥"):
        text = func.replace(text, literal_column("N'" + character + "'"), literal_column("N''"))
    # These are fixed server constants, never request/model values.
    codes = tuple(literal_column("N'" + code.lower() + "'") for code in sorted(CURRENCY_CODES))
    text = case((func.left(text, literal_column("3")).in_(codes), func.substring(text, literal_column("4"), literal_column("2147483647"))), else_=text)
    suffix_length = case((func.len(text) >= literal_column("3"), func.len(text) - literal_column("3")), else_=literal_column("0"))
    text = case((func.right(text, literal_column("3")).in_(codes), func.left(text, suffix_length)), else_=text)
    as_number = func.try_convert(literal_column("FLOAT"), text)
    prefix = func.left(text, func.nullif(func.len(text), literal_column("0")) - literal_column("1"))
    scaled = func.try_convert(literal_column("FLOAT"), prefix)
    value = case((text.like(literal_column("N'%k'")), scaled * literal_column("1000.0")),
                 (text.like(literal_column("N'%m'")), scaled * literal_column("1000000.0")),
                 (text.like(literal_column("N'%b'")), scaled * literal_column("1000000000.0")), else_=as_number)
    return select(value).select_from(projected).where(projected.c.lead_id == Lead.id).correlate(Lead).scalar_subquery()


def column_for(name: str, numeric: bool = False):
    if name == "row_number":
        return Lead.row_index + 1
    if name == "reviewed":
        return func.coalesce(LeadTracking.reviewed, False)
    if name == "tracking_notes":
        return cast(LeadTracking.notes, UnicodeText)
    if name == "owner_name":
        return TeamMember.name
    if name == "owner_active":
        return TeamMember.active
    if name in {"follow_up_at", "follow_up_completed_at", "follow_up_note"}:
        column = getattr(LeadTracking, name)
        return cast(column, UnicodeText) if name == "follow_up_note" else column
    if name == "follow_up_status":
        return case((LeadTracking.follow_up_at.is_(None), "unscheduled"),
                    (LeadTracking.follow_up_completed_at.isnot(None), "completed"),
                    (LeadTracking.follow_up_at <= datetime.now(timezone.utc).replace(tzinfo=None), "due"), else_="upcoming")
    if name == "evidence_checked_at":
        return Lead.evidence_checked_at
    if name == "evidence_age_days":
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        elapsed = func.datediff_big(literal_column("second"), Lead.evidence_checked_at, now)
        return case((Lead.evidence_checked_at <= now, cast(func.floor(cast(elapsed, Float) / 86400), Integer)), else_=None)
    if name == "evidence_status":
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return case((or_(Lead.evidence_checked_at.is_(None), Lead.evidence_checked_at > now), "unknown"),
                    (Lead.evidence_checked_at >= now - timedelta(days=90), "fresh"), else_="stale")
    if name == "refresh_status":
        return func.coalesce((select(LeadRefresh.status).where(LeadRefresh.lead_id == Lead.id)
            .order_by(case((LeadRefresh.status.in_(("queued", "running")), 0), else_=1),
                      LeadRefresh.created_at.desc(), LeadRefresh.id.desc()).limit(1).correlate(Lead).scalar_subquery()), "Not requested")
    if name == "website_ingest_status":
        return func.coalesce((select(CompanyScrape.ingest_status).where(CompanyScrape.lead_id == Lead.id,
            CompanyScrape.job_id == Lead.job_id).order_by(CompanyScrape.scraped_at.desc(), CompanyScrape.id.desc()
            ).limit(1).correlate(Lead).scalar_subquery()), "Not requested")
    if name == "employment_status":
        return func.coalesce(confirmed_fact_value("employment"), "unclear")
    if name in SCORE_FIELDS:
        column = getattr(FinalScore, name)
        return cast(column, UnicodeText) if name in {"reasoning", "growth_signals", "outreach_intelligence"} else column
    if name in LEAD_FIELDS:
        if numeric and name in {"employee_count", "annual_revenue"}:
            return _numeric_text(name)
        column = getattr(Lead, name)
        researched = {"industry": "industry", "employee_count": "employee_count", "annual_revenue": "annual_revenue", "company_hq_location": "location"}
        if name in researched:
            column = func.coalesce(confirmed_fact_value(researched[name]), column)
        if name in {"notes", "company_description", "technologies_used"}:
            column = cast(column, UnicodeText)
        return column
    raise ValueError("Unsupported chat field")


def _missing(column):
    return or_(column.is_(None), func.ltrim(func.rtrim(column)) == "")


def _missing_contact(column):
    return or_(column.is_(None), func.lower(func.ltrim(func.rtrim(column))).in_(
        ("", "none", "null", "n/a", "na", "unknown", "not available", "-", "--")))


def _predicate(item: dict):
    name, operator, value = item["field"], item["op"], item.get("value")
    column = column_for(name, numeric=operator in {"gt", "gte", "lt", "lte"}
                        or isinstance(value, (int, float)) and not isinstance(value, bool))
    if operator in {"is_missing", "is_present"}:
        if name == "owner_name":
            missing = LeadTracking.owner_id.is_(None)
        elif name == "phone_number":
            missing = and_(_missing_contact(Lead.phone_number), _missing_contact(Lead.direct_phone_number))
        elif name in {"email_address", "direct_phone_number", "hq_phone_number"}:
            missing = _missing_contact(column)
        elif name == "tracking_notes":
            missing = _missing(cast(LeadTracking.notes, UnicodeText))
        elif name in {"total_score", "icp_score", "growth_score", "contact_score", "row_number", "reviewed", "owner_active", "evidence_age_days"} | DATE_FIELDS:
            missing = column.is_(None)
        else:
            missing = _missing(column)
        return missing if operator == "is_missing" else ~missing
    if name in DATE_FIELDS:
        if "T" in value or " " in value:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        else:
            column, value = cast(column, Date), date.fromisoformat(value)
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


def _record(lead: Lead, icp_input: str | None, fields=None, refresh_status=None, website=None, ingest_status=None) -> dict:
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
    runs = (lead.refresh_requests or []) if refresh_status is None else []
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
        "refresh_status": refresh_status if refresh_status is not None else latest_refresh.status if latest_refresh else "Not requested",
    })
    stages = {validation.stage: json_object(validation.raw_output) for validation in lead.validation_results}
    try:
        contact = saved_contact({"Email Address": lead.email_address, "Phone Number": lead.phone_number,
                                 "Direct Phone Number": lead.direct_phone_number}, stages.get("contact", {}))
        result["icp_validation_proofs"] = proof_rows(stages.get("web", {}), stages.get("icp", {}), contact, icp_input)
    except (TypeError, ValueError, AttributeError, OverflowError):
        contact = {}
        result["icp_validation_proofs"] = []
        result["evidence_warning"] = "Some older saved evidence could not be interpreted; inspect or refresh this lead's proofs."
    contact_proof = next((proof for proof in result["icp_validation_proofs"] if proof.get("field") == "contact_method"), {})
    result["contact_availability"] = contact_proof.get("value", "Unclear — saved contact evidence is unavailable")
    result["contact_methods"] = contact.get("methods", []) if isinstance(contact.get("methods"), list) else []
    employment = next((proof for proof in result["icp_validation_proofs"] if proof.get("field") == "employment"), {})
    result["employment_status"] = employment.get("value") if employment.get("status") == "confirmed" else "unclear"
    # Researched facts must not disappear just because an upload cell was blank.
    mappings = {"industry": "industry", "employee_count": "employee_count", "annual_revenue": "annual_revenue", "company_hq_location": "location"}
    for column, fact_name in mappings.items():
        fact = next((proof for proof in result["icp_validation_proofs"] if proof["field"] == fact_name), {})
        if fact.get("status") == "confirmed":
            result[column] = fact.get("value")
        elif fact.get("status") == "conflicting":
            result[column] = "Unclear — sources disagree: " + str(fact.get("value") or "")
        elif result.get(column):
            result[column] = str(result[column]) + " (uploaded value; not independently verified)"
    for proof in result["icp_validation_proofs"]:
        for name in ("requirement", "reasoning", "explanation", "value", "label", "checked_at"):
            proof[name] = _bounded(proof.get(name), 700)
        sources = proof.get("sources") if isinstance(proof.get("sources"), list) else []
        proof["sources"] = [{"url": source["url"], "title": _bounded(source.get("title"), 160),
                              "excerpt": _bounded(source.get("excerpt"), 350)}
                             for source in sources[:3] if isinstance(source, dict) and safe_url(source.get("url"))]
    # Include saved stage verdicts/verification dates, never private prompts or arbitrary raw output.
    result["validation_results"] = [{"stage": validation.stage, "passed": validation.passed,
                                      "score": validation.score, "reasoning": _bounded(validation.reasoning),
                                      "checked_at": str(validation.created_at) if validation.created_at else None}
                                     for validation in lead.validation_results]
    if contact:
        result["contact_verification"] = {
            name: _bounded(contact.get(name), 600)
            for name in ("email_valid", "email_verified", "phone_valid", "linkedin_valid", "reasoning")
            if name in contact
        }
    scrape = website
    result["website_ingest_status"] = ingest_status or (getattr(scrape, "ingest_status", None) if scrape else None) or "Not requested"
    if scrape and scrape.clean_text:
        result["stored_website_context"] = {"text": _bounded(scrape.clean_text, 1800),
                                            "website_url": safe_url(scrape.website_url),
                                            "checked_at": str(scrape.scraped_at) if scrape.scraped_at else None,
                                            "ingest_status": result["website_ingest_status"],
                                            "historical": bool(lead.evidence_checked_at and scrape.scraped_at and utc(scrape.scraped_at) < utc(lead.evidence_checked_at))}
    sources = []
    relevant = {mappings.get(name) for name in fields or []} - {None}
    if fields and "contact_availability" in fields:
        relevant.add("contact_method")
    if fields and set(fields) & {"email_address", "phone_number", "direct_phone_number"}:
        relevant.add("contact_method")
    if fields and set(fields) & {"growth_signals", "growth_score"}:
        relevant.add("growth")
    for proof in result["icp_validation_proofs"]:
        if relevant and proof["field"] not in relevant:
            continue
        for source in proof.get("sources", []):
            sources.append({"company_name": lead.company_name, "website_url": source["url"],
                            "title": source.get("title"), "excerpt": source.get("excerpt")})
    for validation in (lead.validation_results if not sources and (not fields or "company_description" in fields) else []):
        raw_sources = stages.get(validation.stage, {}).get("sources", [])
        try:
            saved_sources = json.loads(validation.sources_json or "[]")
        except (TypeError, ValueError):
            saved_sources = []
        for url in clean_sources(raw_sources) + clean_sources(saved_sources):
            sources.append({"company_name": lead.company_name, "website_url": url})
    if scrape and safe_url(scrape.website_url) and (not fields or "company_description" in fields):
        sources.append({"company_name": lead.company_name, "website_url": safe_url(scrape.website_url),
                        "title": "Stored company website"})
    result["_sources"] = sources
    return result


def _date(value):
    return utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def base_query(db, job_id: str, plan: QueryPlan, scope: dict):
    """One mandatory scope/compiler shared by records, aggregates and evidence."""
    query = (db.query(Lead).outerjoin(FinalScore, FinalScore.lead_id == Lead.id)
             .outerjoin(LeadTracking, LeadTracking.id == Lead.tracking_id)
             .outerjoin(TeamMember, TeamMember.id == LeadTracking.owner_id)
             .filter(Lead.job_id == job_id))
    if scope.get("companies"):
        query = query.filter(Lead.company_name.in_(scope["companies"]))
    if scope.get("person_ids"):
        query = query.filter(Lead.id.in_(scope["person_ids"]))
    if plan.row_numbers:
        query = query.filter(Lead.row_index.in_([number - 1 for number in plan.row_numbers]))
    for item in plan.filters:
        query = query.filter(_predicate(item))
    if plan.filter_groups:
        query = query.filter(or_(*(and_(*(_predicate(item) for item in group)) for group in plan.filter_groups)))
    return query.enable_eagerloads(False)


def retrieve(db, job_id: str, plan: QueryPlan, scope: dict) -> dict:
    """Every count/filter/order is evaluated over the entire job before paging."""
    query = base_query(db, job_id, plan, scope)
    total = query.with_entities(func.count(Lead.id)).scalar() or 0
    context = {"matched_count_total": total, "offset": plan.offset, "limit": plan.limit,
               "matched_contacts": [], "companies": [], "sources": [],
               "query": plan.to_dict(), "scope": {key: value for key, value in scope.items() if value},
               "numeric_filter_note": any(item["field"] in {"employee_count", "annual_revenue"}
                                          and isinstance(item.get("value"), (int, float)) for item in plan.filters + [item for group in plan.filter_groups for item in group])}
    if plan.row_numbers and total < len(plan.row_numbers):
        saved = {row[0] + 1 for row in db.query(Lead.row_index).filter(Lead.job_id == job_id,
            Lead.row_index.in_([number - 1 for number in plan.row_numbers])).all()}
        matched = {row[0] + 1 for row in query.with_entities(Lead.row_index).all()}
        context["missing_requested_rows"] = sorted(set(plan.row_numbers) - saved)
        context["rows_excluded_by_filters"] = sorted(saved - matched)
    if plan.row_numbers and not total:
        count, first, last = db.query(func.count(Lead.id), func.min(Lead.row_index), func.max(Lead.row_index)).filter(Lead.job_id == job_id).one()
        exists = db.query(Lead.id).filter(Lead.job_id == job_id, Lead.row_index.in_([number - 1 for number in plan.row_numbers])).first()
        context["row_coverage"] = {"count": int(count or 0), "first": first + 1 if first is not None else None,
                                   "last": last + 1 if last is not None else None, "requested_exist": bool(exists)}
    if plan.action == "count":
        return context
    if plan.action == "aggregate":
        metric_expression = column_for(plan.metric, numeric=plan.metric in {"employee_count", "annual_revenue"})
        projected_fields = [Lead.id.label("lead_id"), metric_expression.label("metric_value")]
        if plan.group_by:
            projected_fields.append(column_for(plan.group_by).label("group_value"))
        # SQL Server disallows aggregate/GROUP BY operands containing scalar
        # subqueries. Project sourced facts/workflow expressions first, then
        # aggregate their derived-table columns in an outer query.
        projected = query.with_entities(*projected_fields).subquery()
        metric = projected.c.metric_value
        expressions = {"avg": func.avg, "sum": func.sum, "min": func.min, "max": func.max,
                       "count_distinct": lambda column: func.count(func.distinct(column))}
        aggregate = expressions[plan.aggregate](metric)
        context["aggregate"] = {"operation": plan.aggregate, "metric": plan.metric, "group_by": plan.group_by,
                                "ignores_missing_values": True, "rows": []}
        if plan.group_by:
            group = projected.c.group_value
            grouped = db.query(group.label("group_value"), aggregate.label("value"),
                func.count(projected.c.lead_id).label("lead_count")).select_from(projected).group_by(group)
            group_count = grouped.count()
            groups = grouped.order_by(case((aggregate.is_(None), 1), else_=0),
                aggregate.desc() if plan.direction == "desc" else aggregate.asc(), group.asc()
            ).offset(plan.offset).limit(plan.limit).all()
            context["aggregate"]["rows"] = [{"group": name, "value": _sql_value(value), "lead_count": int(count)}
                                              for name, value, count in groups]
            context["aggregate"]["group_count"] = group_count
            context["has_more"] = plan.offset + len(groups) < group_count
        else:
            value, populated = db.query(aggregate, func.count(metric)).select_from(projected).one()
            context["aggregate"].update(value=_sql_value(value), populated_records=int(populated or 0))
        if plan.metric in {"employee_count", "annual_revenue"}:
            context["numeric_filter_note"] = True
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
    if plan.sort_by == "priority":
        order = case((FinalScore.priority == "High", 3), (FinalScore.priority == "Medium", 2), (FinalScore.priority == "Low", 1), else_=0)
    leads = (query.enable_eagerloads(True)
             .options(selectinload(Lead.final_score).load_only(*(getattr(FinalScore, field) for field in SCORE_FIELDS)), selectinload(Lead.validation_results),
                      noload(Lead.company_scrape), selectinload(Lead.tracking),
                      noload(Lead.refresh_requests))
             .order_by(case((order.is_(None), 1), else_=0), order.desc() if plan.direction == "desc" else order.asc(),
                       FinalScore.total_score.desc() if plan.sort_by == "priority" else Lead.row_index.asc(), Lead.row_index.asc(), Lead.id.asc())
             .offset(plan.offset).limit(plan.limit).all())
    icp_input = db.query(Job.icp_prompt).filter(Job.id == job_id).scalar()
    refreshes = latest_refreshes(db, [lead.id for lead in leads])
    need_site = not plan.fields or plan.compose or plan.semantic_query or bool(set(plan.fields) & {"company_description", "stored_website_context"})
    websites = latest_scrapes(db, [lead.id for lead in leads], job_id) if need_site else {}
    indexing = latest_scrapes(db, [lead.id for lead in leads], job_id, include_empty=True)
    rows = [_record(lead, icp_input, plan.fields, refreshes.get(lead.id, "Not requested"), websites.get(lead.id),
                    indexing[lead.id].ingest_status if lead.id in indexing else None) for lead in leads]
    sources = {}
    for row in rows:
        for source in row.pop("_sources", []):
            sources.setdefault(citation_key(source["website_url"]), source)
    context["sources"] = list(sources.values())[:8]
    context["matched_contacts"] = rows
    context["has_more"] = plan.offset + len(rows) < total
    return context


def _sql_value(value):
    if isinstance(value, datetime):
        return _date(value)
    if isinstance(value, float):
        return round(value, 4)
    return value


def latest_refreshes(db, ids):
    if not ids:
        return {}
    latest = select(LeadRefresh.lead_id, LeadRefresh.status, func.row_number().over(
        partition_by=LeadRefresh.lead_id,
        order_by=(case((LeadRefresh.status.in_(("queued", "running")), 0), else_=1),
                  LeadRefresh.created_at.desc(), LeadRefresh.id.desc())).label("position")
    ).where(LeadRefresh.lead_id.in_(ids)).subquery()
    return dict(db.execute(select(latest.c.lead_id, latest.c.status).where(latest.c.position == 1)).all())


def latest_scrapes(db, ids, job_id, include_empty=False):
    if not ids:
        return {}
    ranked_query = select(CompanyScrape.id, func.row_number().over(partition_by=CompanyScrape.lead_id,
        order_by=(CompanyScrape.scraped_at.desc(), CompanyScrape.id.desc())).label("position")
    ).where(CompanyScrape.lead_id.in_(ids), CompanyScrape.job_id == job_id)
    if not include_empty:
        ranked_query = ranked_query.where(CompanyScrape.clean_text.isnot(None), func.len(cast(CompanyScrape.clean_text, UnicodeText)) > 0)
    ranked = ranked_query.subquery()
    query = db.query(CompanyScrape).join(ranked, ranked.c.id == CompanyScrape.id).filter(ranked.c.position == 1)
    if include_empty:
        query = query.options(load_only(CompanyScrape.lead_id, CompanyScrape.ingest_status))
    rows = query.all()
    return {row.lead_id: row for row in rows}
