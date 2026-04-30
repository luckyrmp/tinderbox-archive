"""Embed worker: stream messages and artifacts, batch-embed via Ollama,
batch-upsert into tinderbox.embeddings idempotently.

Idempotency: the unique index on
  (source_type, source_id, chunk_index, embedding_model)
means re-running the worker is a no-op for already-embedded chunks. We
pre-filter with a "what's already embedded" query before each batch so we
don't waste Ollama calls.

Skip rules:
  - Empty content (whitespace-only) → no chunks → skipped.
  - The dedicated "is the message truncated" check is done at chunk time; we
    embed `messages.content` (rendered text, preserved in full), not raw_content,
    so truncation has no impact on the embed pass.

Pipelining order:
  1. Pull a page of source rows that don't yet have embeddings for this model.
  2. For each source, build chunks via chunking.chunk_*().
  3. Concatenate all chunk texts into one batch.
  4. embed_batch() the texts.
  5. upsert one batch of embedding rows.
  6. Repeat.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from tinderbox import db
from tinderbox.config import Config
from tinderbox.embed.chunking import (
    Chunk,
    chunk_message,
    chunk_artifact,
)
from tinderbox.embed.ollama_client import embed_batch, OllamaEmbeddingError
from tinderbox.ingest.retry import with_retry

logger = logging.getLogger("tinderbox.embed.worker")


# Batch sizes — we tune toward Supabase REST limits and Ollama warm-up.
SOURCE_PAGE_SIZE = 50         # rows per DB page when iterating sources
OLLAMA_BATCH_SIZE = 32        # texts per Ollama /api/embed call
DB_UPSERT_BATCH_SIZE = 128    # embedding rows per Supabase upsert call


@dataclass
class EmbedCounters:
    sources_seen: int = 0
    sources_skipped_empty: int = 0
    sources_skipped_already_embedded: int = 0
    chunks_embedded: int = 0
    chunks_failed: int = 0   # individual rows that wouldn't embed
    embedding_calls: int = 0
    upsert_calls: int = 0


def _embed_batch_with_fallback(batch: list[tuple[str, str, "Chunk"]],
                                config: Config, http: httpx.Client,
                                counters: EmbedCounters) -> list[list[float] | None]:
    """Try to embed `batch` as one batch. On failure, retry one-at-a-time.

    Returns a list of vectors (or None for items that wouldn't embed).
    Same length as `batch`.
    """
    texts = [c.chunk_text for (_, _, c) in batch]
    try:
        vectors = with_retry(
            lambda: embed_batch(texts, config, client=http),
            op_name=f"ollama batch (n={len(texts)})",
        )
        counters.embedding_calls += 1
        return vectors
    except OllamaEmbeddingError as e:
        logger.warning("Batch failed (%s); falling back to one-at-a-time", str(e)[:120])

    # One-at-a-time fallback.
    out: list[list[float] | None] = []
    for i, text in enumerate(texts):
        try:
            v = with_retry(
                lambda t=text: embed_batch([t], config, client=http),
                op_name=f"ollama single fallback i={i}",
                max_attempts=2,
            )
            counters.embedding_calls += 1
            out.append(v[0])
        except OllamaEmbeddingError as e:
            counters.chunks_failed += 1
            src_type, src_id, chunk = batch[i]
            logger.warning(
                "Single-row embed failed (%s/%s chunk=%d): %s",
                src_type, src_id, chunk.chunk_index, str(e)[:120],
            )
            out.append(None)
    return out


def _existing_chunk_indices(source_type: str, source_id: str,
                            embedding_model: str) -> set[int]:
    """Return chunk_index values already embedded for this source+model."""
    res = (
        db.table("embeddings")
        .select("chunk_index")
        .eq("source_type", source_type)
        .eq("source_id", source_id)
        .eq("embedding_model", embedding_model)
        .execute()
    )
    return {r["chunk_index"] for r in (res.data or [])}


def _iter_message_sources(config: Config) -> "Iterator[dict]":
    """Yield {id, content} dicts for messages with non-empty content."""
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            db.table("messages")
            .select("id,content")
            .gt("id", last_id)
            .order("id", desc=False)
            .limit(SOURCE_PAGE_SIZE)
            .execute()
        )
        rows = page.data or []
        if not rows:
            break
        for row in rows:
            yield row
        last_id = rows[-1]["id"]


def _iter_artifact_sources(config: Config) -> "Iterator[dict]":
    """Yield {id, title, content} dicts for artifacts."""
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            db.table("artifacts")
            .select("id,title,content")
            .gt("id", last_id)
            .order("id", desc=False)
            .limit(SOURCE_PAGE_SIZE)
            .execute()
        )
        rows = page.data or []
        if not rows:
            break
        for row in rows:
            yield row
        last_id = rows[-1]["id"]


def _flush_pending(pending: list[tuple[str, str, Chunk]],
                   config: Config, http: httpx.Client,
                   counters: EmbedCounters) -> None:
    """Embed pending chunks in Ollama batches, then upsert in DB batches."""
    if not pending:
        return

    # Embed in OLLAMA_BATCH_SIZE batches.
    embedded_rows: list[dict] = []
    for batch_start in range(0, len(pending), OLLAMA_BATCH_SIZE):
        batch = pending[batch_start:batch_start + OLLAMA_BATCH_SIZE]
        vectors = _embed_batch_with_fallback(batch, config, http, counters)

        for (source_type, source_id, chunk), vec in zip(batch, vectors):
            if vec is None:
                continue   # this single row failed, skip it
            embedded_rows.append({
                "source_type": source_type,
                "source_id": source_id,
                "chunk_index": chunk.chunk_index,
                "chunk_text": chunk.chunk_text,
                "embedding": vec,
                "embedding_model": config.embedding_model,
                "embedding_dim": config.embedding_dim,
            })

    # Upsert in DB batches. ignore_duplicates=True so re-runs no-op cleanly
    # even if a concurrent run inserted the same (source, chunk, model) tuple.
    for batch_start in range(0, len(embedded_rows), DB_UPSERT_BATCH_SIZE):
        batch = embedded_rows[batch_start:batch_start + DB_UPSERT_BATCH_SIZE]

        def _upsert():
            return (
                db.table("embeddings")
                .upsert(
                    batch,
                    on_conflict="source_type,source_id,chunk_index,embedding_model",
                    ignore_duplicates=True,
                )
                .execute()
            )
        try:
            result = with_retry(
                _upsert, op_name=f"upsert embeddings batch {batch_start}"
            )
            counters.upsert_calls += 1
            counters.chunks_embedded += len(result.data or [])
        except Exception as e:
            logger.error("Embedding upsert failed; skipping batch: %s", e)


def embed_messages(config: Config, http: httpx.Client | None = None,
                   limit: int | None = None) -> EmbedCounters:
    """Embed every message that doesn't yet have an embedding for this model.

    `limit` caps the number of source rows processed (handy for smoke tests).
    """
    counters = EmbedCounters()
    own_http = http is None
    if http is None:
        http = httpx.Client(timeout=120.0)

    pending: list[tuple[str, str, Chunk]] = []  # (source_type, source_id, chunk)

    try:
        for row in _iter_message_sources(config):
            if limit is not None and counters.sources_seen >= limit:
                break
            counters.sources_seen += 1
            content = row.get("content") or ""
            if not content.strip():
                counters.sources_skipped_empty += 1
                continue

            existing = _existing_chunk_indices(
                "message", row["id"], config.embedding_model
            )
            chunks = chunk_message(content)
            new_chunks = [c for c in chunks if c.chunk_index not in existing]
            if not new_chunks:
                counters.sources_skipped_already_embedded += 1
                continue

            for chunk in new_chunks:
                pending.append(("message", row["id"], chunk))

            # Flush periodically.
            if len(pending) >= OLLAMA_BATCH_SIZE * 4:
                _flush_pending(pending, config, http, counters)
                pending.clear()

            if counters.sources_seen % 200 == 0:
                logger.info(
                    "messages: seen=%d embedded_chunks=%d empty=%d already=%d",
                    counters.sources_seen, counters.chunks_embedded,
                    counters.sources_skipped_empty,
                    counters.sources_skipped_already_embedded,
                )

        # Final flush.
        _flush_pending(pending, config, http, counters)
    finally:
        if own_http:
            http.close()

    return counters


def embed_artifacts(config: Config, http: httpx.Client | None = None,
                    limit: int | None = None) -> EmbedCounters:
    """Embed every artifact (summary chunk + body chunks)."""
    counters = EmbedCounters()
    own_http = http is None
    if http is None:
        http = httpx.Client(timeout=120.0)

    pending: list[tuple[str, str, Chunk]] = []

    try:
        for row in _iter_artifact_sources(config):
            if limit is not None and counters.sources_seen >= limit:
                break
            counters.sources_seen += 1
            content = row.get("content") or ""
            title = row.get("title") or ""
            if not content.strip() and not title.strip():
                counters.sources_skipped_empty += 1
                continue

            existing = _existing_chunk_indices(
                "artifact", row["id"], config.embedding_model
            )
            chunks = chunk_artifact(title, content)
            new_chunks = [c for c in chunks if c.chunk_index not in existing]
            if not new_chunks:
                counters.sources_skipped_already_embedded += 1
                continue

            for chunk in new_chunks:
                pending.append(("artifact", row["id"], chunk))

            if len(pending) >= OLLAMA_BATCH_SIZE * 4:
                _flush_pending(pending, config, http, counters)
                pending.clear()

            if counters.sources_seen % 50 == 0:
                logger.info(
                    "artifacts: seen=%d embedded_chunks=%d already=%d",
                    counters.sources_seen, counters.chunks_embedded,
                    counters.sources_skipped_already_embedded,
                )

        _flush_pending(pending, config, http, counters)
    finally:
        if own_http:
            http.close()

    return counters


def embed_all(config: Config, limit: int | None = None) -> dict:
    """Run embed_messages + embed_artifacts. Returns combined counters."""
    http = httpx.Client(timeout=120.0)
    try:
        msg_counters = embed_messages(config, http=http, limit=limit)
        logger.info("messages embed pass complete: %s", msg_counters)
        art_counters = embed_artifacts(config, http=http, limit=limit)
        logger.info("artifacts embed pass complete: %s", art_counters)
    finally:
        http.close()

    return {"messages": msg_counters, "artifacts": art_counters}
