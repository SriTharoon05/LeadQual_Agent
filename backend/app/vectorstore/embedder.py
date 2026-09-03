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

from openai import AzureOpenAI

from app.config import settings

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
def embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of strings → list of vectors (same order)."""
    if not texts:
        return []
    client = _client()
    logger.warning(f"[embed] endpoint={settings.AZURE_OPENAI_ENDPOINT!r}")
    logger.warning(f"[embed] api_version={settings.AZURE_OPENAI_API_VERSION!r}")
    logger.warning(f"[embed] deployment={settings.EMBED_DEPLOYMENT!r}")
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        resp = client.embeddings.create(
            model=settings.EMBED_DEPLOYMENT,  
            input=batch,
        )
        out.extend(d.embedding for d in resp.data)
    return out


def embed_one(text: str) -> list[float]:
    """Embed a single query string."""
    return embed([text])[0] if text and text.strip() else []
