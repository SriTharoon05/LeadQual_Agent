"""
vectorstore/qdrant_store.py
───────────────────────────
Qdrant Cloud wrapper for the chatbot RAG feature.

Design decisions (per Qdrant multitenancy best practice):
  • ONE collection for everything: "company_chunks".
  • Each point is partitioned by a `job_id` payload field (the "session").
  • `job_id` is a TENANT payload index (is_tenant=True) → Qdrant co-locates a
    job's vectors on disk for fast sequential reads.
  • HNSW is built PER TENANT (payload_m=16, m=0) instead of one global graph,
    which massively speeds up ingestion when many jobs share the collection.
  • `company_name` is also indexed so "…for this company" queries pre-filter.

Every read/write is HARD-SCOPED to a job_id, so one session can never see
another session's data.

Env / settings required (add to app/config.py):
    QDRANT_URL            = "https://xxxx.cloud.qdrant.io:6333"
    QDRANT_API_KEY        = "..."
    QDRANT_COLLECTION     = "company_chunks"
    EMBED_DIM             = 1536          # text-embedding-3-small
"""

import logging
import hashlib
import json
import math
import uuid
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION = settings.QDRANT_COLLECTION


# ── Singleton client ──────────────────────────────────────────────────────────
_client: Optional[QdrantClient] = None


def get_client() -> QdrantClient:
    """Lazy singleton Qdrant Cloud client (thread-safe enough for our worker)."""
    global _client
    if _client is None:
        _client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=30,
            prefer_grpc=False,
        )
    return _client


# ── One-time collection bootstrap ─────────────────────────────────────────────
def ensure_collection() -> None:
    """
    Create the shared collection + payload indexes if they don't exist.
    Call this ONCE at app/worker startup (idempotent).
    """
    client = get_client()

    exists = client.collection_exists(COLLECTION)
    if not exists:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=qm.VectorParams(
                size=settings.EMBED_DIM,
                distance=qm.Distance.COSINE,
            ),
            # Per-tenant HNSW: disable the global graph (m=0), build per group.
            hnsw_config=qm.HnswConfigDiff(payload_m=16, m=0),
        )
        logger.info(f"Created Qdrant collection '{COLLECTION}'")

    # job_id → tenant index (co-locates a session's vectors on disk).
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="job_id",
        field_schema=qm.KeywordIndexParams(
            type=qm.KeywordIndexType.KEYWORD,
            is_tenant=True,
        ),
    )
    # company_name → keyword index for "…for this company" filtering.
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="company_name",
        field_schema=qm.PayloadSchemaType.KEYWORD,
    )
    for field_name in ("lead_id", "generation"):
        client.create_payload_index(collection_name=COLLECTION, field_name=field_name,
                                    field_schema=qm.PayloadSchemaType.KEYWORD)
    logger.info("Qdrant payload indexes ensured (job_id tenant, company_name)")


# ── Ingestion ─────────────────────────────────────────────────────────────────
def upsert_chunks(
    job_id: str,
    lead_id: str,
    company_name: str,
    chunks: list[str],
    vectors: list[list[float]],
    website_url: str = "",
    batch_size: int = 128,
) -> int:
    """
    Insert a company's chunk embeddings, tagged with the session job_id.
    Batches at 128 points (Qdrant sweet spot 64–256).
    Returns number of points written.
    """
    if len(chunks) != len(vectors):
        raise ValueError("Every text chunk must have exactly one embedding")
    if not chunks:
        return 0
    if batch_size < 1 or batch_size > 256:
        raise ValueError("Vector batch size must be between 1 and 256")
    if any(len(vector) != settings.EMBED_DIM or any(not math.isfinite(value) for value in vector)
           for vector in vectors):
        raise ValueError("Embedding dimensions/values do not match the configured collection")
    job_id, lead_id = str(job_id), str(lead_id)
    generation = hashlib.sha256(json.dumps([website_url, chunks], ensure_ascii=False).encode()).hexdigest()

    client = get_client()
    points: list[qm.PointStruct] = []
    for index, (text, vec) in enumerate(zip(chunks, vectors)):
        points.append(
            qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"ql:{job_id}:{lead_id}:{generation}:{index}")),
                vector=vec,
                payload={
                    "job_id":       job_id,        # ← session scope
                    "lead_id":      lead_id,
                    "company_name": company_name,  # ← company scope
                    "website_url":  website_url,
                    "chunk_text":   text,
                    "generation":   generation,
                    "chunk_index":  index,
                },
            )
        )

    written = 0
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=COLLECTION, points=points[i:i + batch_size], wait=True)
        written += len(points[i:i + batch_size])

    # Only retire an older generation after every new chunk was acknowledged.
    # Embedding workers serialize by lead so an older task cannot erase a newer
    # generation. This also removes legacy randomly named duplicate points.
    client.delete(collection_name=COLLECTION, points_selector=qm.FilterSelector(filter=qm.Filter(
        must=[qm.FieldCondition(key="job_id", match=qm.MatchValue(value=job_id)),
              qm.FieldCondition(key="lead_id", match=qm.MatchValue(value=lead_id))],
        must_not=[qm.FieldCondition(key="generation", match=qm.MatchValue(value=generation))],
    )), wait=True)

    logger.info(f"Upserted {written} chunks for '{company_name}' (job={job_id})")
    return written


# ── Retrieval (always scoped to a job_id) ─────────────────────────────────────
def search(
    job_id: str,
    query_vector: list[float],
    company_name: Optional[str] = None,
    limit: int = 6,
    *,
    lead_ids: Optional[list[str]] = None,
    company_names: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> list[dict]:
    """
    Semantic search WITHIN a single session (job_id). Optionally narrow to one
    company. Returns [{score, chunk_text, company_name, website_url}, ...].
    """
    client = get_client()

    must = [qm.FieldCondition(key="job_id", match=qm.MatchValue(value=job_id))]
    if company_name:
        must.append(
            qm.FieldCondition(key="company_name",
                              match=qm.MatchValue(value=company_name))
        )
    if company_names:
        must.append(qm.FieldCondition(key="company_name", match=qm.MatchAny(any=list(dict.fromkeys(company_names)))))
    if lead_ids:
        must.append(qm.FieldCondition(key="lead_id", match=qm.MatchAny(any=list(dict.fromkeys(str(value) for value in lead_ids)))))

    hits = client.query_points(
        collection_name=COLLECTION,
        query=query_vector,
        query_filter=qm.Filter(must=must),
        limit=min(40, max(1, limit)),
        with_payload=True,
        timeout=timeout,
    ).points

    return [
        {
            "score":        h.score,
            "chunk_text":   h.payload.get("chunk_text", ""),
            "company_name": h.payload.get("company_name", ""),
            "website_url":  h.payload.get("website_url", ""),
            "lead_id":      h.payload.get("lead_id", ""),
            "job_id":       h.payload.get("job_id", ""),
        }
        for h in hits
    ]


def delete_job(job_id: str) -> None:
    """Remove all vectors for a session (call from DELETE /jobs/{job_id})."""
    client = get_client()
    client.delete(
        collection_name=COLLECTION,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="job_id",
                                        match=qm.MatchValue(value=job_id))]
            )
        ),
    )
    logger.info(f"Deleted all Qdrant vectors for job {job_id}")
