"""Bounded hybrid evidence retrieval; semantic samples never become SQL totals."""
import logging
import re
from functools import lru_cache
from time import monotonic
from uuid import UUID

from sqlalchemy import UnicodeText, and_, case, cast, func, or_, select
from sqlalchemy.orm import selectinload, noload

from app.chat.retrieval import _record, base_query, latest_refreshes, latest_scrapes
from app.config import settings
from app.models import CompanyScrape, Job, Lead
from app.validators.evidence import citation_key, safe_url

logger = logging.getLogger(__name__)
_STOP = set("a an the is are do does did it its this that their they these those what which who how why can could would should will i me my you your we us our please show tell give get find list search about for from at of to on in by company companies person contact contacts lead leads records details detail information info data saved stored record row explain more any all have has had offer offers provide provides make makes sell sells".split())


def _lead_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@lru_cache(maxsize=32)
def _cached_query_vector(deployment, dimension, window, question):
    from app.vectorstore.embedder import embed_one
    return tuple(embed_one(question, timeout=8.0))


def _terms(query, scope):
    ignored = {token for name in scope.get("companies", []) for token in re.findall(r"\w+", name.casefold())}
    terms = list(dict.fromkeys(token for token in re.findall(r"[^\W_]+", query.casefold())
                              if len(token) > 2 and token not in _STOP and token not in ignored))
    return terms[:6]


def _excerpt(text, terms):
    text = str(text or "")
    lowered = text.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 250) if positions else 0
    return ("…" if start else "") + text[start:start + 1600] + ("…" if start + 1600 < len(text) else "")


def enrich_evidence(db, job_id, plan, scope, data):
    """Read passages from both stores; hydrate every vector hit through scoped SQL.

    Counts in data describe structured SQL predicates only. Website passages are
    explicitly a ranked sample and do not prove a complete semantic match set.
    """
    question = plan.semantic_query
    if not question:
        return data
    terms = _terms(question, scope)
    vector_hits, vector_error = [], None
    db.commit()  # No SQL transaction/connection waits on embedding or Qdrant.
    try:
        from app.vectorstore.qdrant_store import search
        vector = list(_cached_query_vector(settings.EMBED_DEPLOYMENT, settings.EMBED_DIM,
                                          int(monotonic() // 120), question))
        if len(vector) != settings.EMBED_DIM:
            raise ValueError("Query embedding dimension differs from the collection")
        selected_ids = scope.get("person_ids") or []
        if plan.row_numbers:
            selected_ids = [str(row[0]) for row in base_query(db, job_id, plan, scope).with_entities(Lead.id).all()]
            db.commit()
        if plan.row_numbers and not selected_ids:
            vector_hits = []
        else:
            vector_hits = search(job_id, vector, limit=24,
                company_names=scope.get("companies") or None,
                lead_ids=selected_ids or None, timeout=8)
    except Exception as exc:
        vector_error = type(exc).__name__
        logger.warning("Chat vector evidence unavailable (job=%s, type=%s)", job_id, vector_error)

    candidate_ids = list(dict.fromkeys(lead_id for hit in vector_hits
        if isinstance(hit, dict) and str(hit.get("job_id", "")).lower() == str(job_id).lower()
        and (lead_id := _lead_id(hit.get("lead_id")))))[:24]
    evidence, seen_text, ids = [], set(), []
    can_scan_sql = data.get("matched_count_total", 0) <= 5000 or bool(scope.get("companies") or scope.get("person_ids") or plan.row_numbers)
    if can_scan_sql:
        text = cast(CompanyScrape.clean_text, UnicodeText)
        latest_id = select(CompanyScrape.id).where(CompanyScrape.lead_id == Lead.id,
            CompanyScrape.job_id == job_id, CompanyScrape.clean_text.isnot(None), func.len(text) > 0
            ).order_by(CompanyScrape.scraped_at.desc(), CompanyScrape.id.desc()
            ).limit(1).correlate(Lead).scalar_subquery()
        query = base_query(db, job_id, plan, scope).join(CompanyScrape,
            and_(CompanyScrape.lead_id == Lead.id, CompanyScrape.job_id == job_id, CompanyScrape.id == latest_id))
        if terms:
            checks = [func.lower(text).contains(term, autoescape=True) for term in terms]
            query = query.filter(or_(*checks))
            relevance = sum((case((check, 1), else_=0) for check in checks))
            query = query.order_by(relevance.desc())
        elif not scope.get("companies") and not scope.get("person_ids") and not plan.row_numbers:
            query = query.filter(Lead.id.in_(candidate_ids))
        sql_rows = query.with_entities(Lead.id, Lead.company_name, Lead.contact_name, Lead.row_index,
            CompanyScrape.clean_text, CompanyScrape.website_url, CompanyScrape.scraped_at
        ).order_by(Lead.row_index, Lead.id).limit(8).all()
        for lead_id, company, person, row_index, text_value, url, checked_at in sql_rows:
            snippet = _excerpt(text_value, terms)
            text_key = " ".join(snippet.casefold().split())
            if not snippet.strip() or text_key in seen_text:
                continue
            evidence.append({"lead_id": str(lead_id).lower(), "company_name": company,
                "contact_name": person, "row_number": row_index + 1 if row_index is not None else None,
                "text": snippet, "website_url": safe_url(url), "store": "sql",
                "snapshot_date": checked_at.isoformat() if checked_at else None,
                "evidence_type": "Saved website snapshot, not a new live search"})
            seen_text.add(text_key)
            ids.append(str(lead_id).lower())

    # Verify every point's lead against the SAME job, entity scope, row and SQL
    # filters. Vector payloads alone cannot establish identity or current facts.
    authorized = base_query(db, job_id, plan, scope).filter(Lead.id.in_(candidate_ids)).with_entities(
        Lead.id, Lead.company_name, Lead.contact_name, Lead.row_index).all() if candidate_ids else []
    by_id = {str(row[0]).lower(): row for row in authorized}
    for hit in vector_hits:
        if not isinstance(hit, dict):
            continue
        lead_id = _lead_id(hit.get("lead_id"))
        if lead_id not in by_id or len(evidence) >= 12:
            continue
        snippet = _excerpt(hit.get("chunk_text"), terms)
        key = " ".join(snippet.casefold().split())
        if not snippet.strip() or key in seen_text:
            continue
        _, company, person, row_index = by_id[lead_id]
        evidence.append({"lead_id": lead_id, "company_name": company, "contact_name": person,
            "row_number": row_index + 1 if row_index is not None else None,
            "text": snippet, "website_url": safe_url(hit.get("website_url")), "store": "vector",
            "snapshot_date": None, "relevance_score": hit.get("score"),
            "evidence_type": "Saved vector research passage; original observation date is unknown unless stated in text"})
        seen_text.add(key)
        ids.append(lead_id)
    ids = list(dict.fromkeys(ids))[:12]
    matched = base_query(db, job_id, plan, scope).filter(Lead.id.in_(ids)).enable_eagerloads(True).options(
        selectinload(Lead.final_score), selectinload(Lead.validation_results),
        noload(Lead.company_scrape), selectinload(Lead.tracking), noload(Lead.refresh_requests)
    ).order_by(Lead.row_index.asc(), Lead.id.asc()).limit(12).all() if ids else []
    icp = db.query(Job.icp_prompt).filter(Job.id == job_id).scalar()
    refreshes = latest_refreshes(db, [lead.id for lead in matched])
    websites = latest_scrapes(db, [lead.id for lead in matched], job_id)
    indexing = latest_scrapes(db, [lead.id for lead in matched], job_id, include_empty=True)
    records = [_record(lead, icp, plan.fields, refreshes.get(lead.id, "Not requested"), websites.get(lead.id),
                       indexing[lead.id].ingest_status if lead.id in indexing else None) for lead in matched]
    sources = {citation_key(source["website_url"]): source for source in data.get("sources", []) if safe_url(source.get("website_url"))}
    for record in records:
        for source in record.pop("_sources", []):
            sources.setdefault(citation_key(source["website_url"]), source)
    for passage in evidence:
        if passage["website_url"]:
            sources.setdefault(citation_key(passage["website_url"]), {"company_name": passage["company_name"],
                "website_url": passage["website_url"], "title": f"Stored {passage['company_name'] or 'company'} research",
                "excerpt": passage["text"][:500]})
    data.update(semantic_evidence=evidence, semantic_results_exhaustive=False,
        semantic_evidence_count=len(evidence), semantic_store_status="unavailable" if vector_error else "searched",
        matched_contacts=records or (data.get("matched_contacts", []) if data.get("matched_count_total", 0) <= 3 else []),
        sources=list(sources.values())[:12], has_more=False)
    if not can_scan_sql:
        data["text_search_note"] = "SQL website-text fallback was not scanned across more than 5,000 leads; narrow to a company/row for that fallback."
    db.commit()
    return data
