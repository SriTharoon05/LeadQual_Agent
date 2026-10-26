"""
vectorstore/embedder.py
───────────────────────
Chunking + embedding helpers for the RAG ingestion pipeline.

  chunk_text(text)  → list[str]      word-based chunks with overlap
  embed(texts)      → list[vector]   batched Azure/OpenAI embeddings

Sizing: ~600 tokens/chunk with ~100 overlap is the RAG sweet spot for
website copy. We approximate tokens with words (~0.75 token/word) to avoid a
tokenizer dependency; tune CHUNK_WORDS if you prefer tiktoken.
"""

import logging
import math

from openai import AzureOpenAI

from app.config import settings
from app.admin.billing import metered_call

logger = logging.getLogger(__name__)

CHUNK_WORDS   = 450      # ≈ 600 tokens
OVERLAP_WORDS = 75       # ≈ 100 tokens
EMBED_BATCH   = 96       # inputs per embeddings API call


# ── Embedding client (reuse your existing Azure OpenAI setup) ─────────────────
_embed_client: AzureOpenAI | None = None


def _client() -> AzureOpenAI:
    global _embed_client
    if _embed_client is None:
        _embed_client = AzureOpenAI(
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            timeout=60.0,
            max_retries=0,
        )
    return _embed_client


# ── Chunking ──────────────────────────────────────────────────────────────────
def chunk_text(text: str) -> list[str]:
    """Split cleaned text into overlapping word windows. Empty → []."""
    if not text or not text.strip():
        return []
    words = text.split()
    if len(words) <= CHUNK_WORDS:
        return [" ".join(words)]

    chunks: list[str] = []
    step = CHUNK_WORDS - OVERLAP_WORDS
    for start in range(0, len(words), step):
        window = words[start:start + CHUNK_WORDS]
        if window:
            chunks.append(" ".join(window))
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


# ── Embedding ─────────────────────────────────────────────────────────────────
def embed(texts: list[str], *, job_id=None, lead_id=None) -> list[list[float]]:
    """Batch-embed a list of strings → list of vectors (same order)."""
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding inputs must be nonempty text strings")
    client = _client()
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        with metered_call(stage="embedding", module="vectorstore.embedder", kind="embedding",
                request_model=settings.EMBED_DEPLOYMENT, job_id=job_id, lead_id=lead_id) as meter:
            resp = client.embeddings.create(model=settings.EMBED_DEPLOYMENT, input=batch)
            meter.response = resp
        out.extend(_validated_embeddings(resp.data, len(batch)))
    return out


def _validated_embeddings(data, count):
    rows = list(data or [])
    if len(rows) != count:
        raise ValueError("Embedding response count does not match input count")
    indices = [getattr(row, "index", None) for row in rows]
    if any(isinstance(index, bool) or not isinstance(index, int) for index in indices) or sorted(indices) != list(range(count)):
        raise ValueError("Embedding response indices are incomplete or duplicated")
    vectors = []
    for row in sorted(rows, key=lambda item: item.index):
        vector = getattr(row, "embedding", None)
        if (not isinstance(vector, list) or len(vector) != settings.EMBED_DIM
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)):
            raise ValueError("Embedding response has an invalid vector or dimension")
        vectors.append(vector)
    return vectors


def embed_one(text: str, *, timeout: float | None = None) -> list[float]:
    """Embed a single query string."""
    if timeout is not None and text and text.strip():
        with metered_call(stage="query_embedding", module="vectorstore.embedder", kind="embedding",
                request_model=settings.EMBED_DEPLOYMENT) as meter:
            response = _client().with_options(timeout=timeout, max_retries=0).embeddings.create(
                model=settings.EMBED_DEPLOYMENT, input=[text[:4000]])
            meter.response = response
        return _validated_embeddings(response.data, 1)[0]
    return embed([text])[0] if text and text.strip() else []
