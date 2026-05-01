"""Hybrid search: embed the query, call tinderbox.hybrid_search RPC, return rows.

Cached: results live in ~/shared/cache.db with a 300s TTL (per shared
DEFAULT_TTLS for `tinderbox_search:`). Cache miss runs the full pipeline
(embed → RPC → snippet derivation). Cache hit returns the cached list and
reports the cached embed/RPC latency for transparency.
"""

import logging
import time
from typing import Any

import httpx

from tinderbox import cache, db
from tinderbox.config import Config
from tinderbox.embed.ollama_client import embed_batch

logger = logging.getLogger("tinderbox.search.hybrid")


def hybrid_search(query_text: str, config: Config,
                  limit: int = 10,
                  cosine_filter: float = 0.6,
                  use_cache: bool = True) -> tuple[list[dict[str, Any]], int]:
    """Run a hybrid search. Returns (results, latency_ms).

    Each result dict has the columns from tinderbox.hybrid_search RETURNS TABLE
    plus a derived `snippet` field built from chunk_text.

    use_cache=False forces a fresh fetch (and refreshes the cache).
    """
    start = time.monotonic()
    key = cache.search_key(query_text, limit, cosine_filter)

    def _fetch() -> dict[str, Any]:
        # 1. Embed the query.
        with httpx.Client(timeout=60.0) as http:
            vectors = embed_batch([query_text], config, client=http)
        query_embedding = vectors[0]

        # 2. Call the server-side hybrid_search RPC.
        rpc = (
            db.get_client()
            .schema(config.schema)
            .rpc(
                "hybrid_search",
                {
                    "query_embedding": query_embedding,
                    "query_text": query_text,
                    "result_limit": limit,
                    "cosine_filter": cosine_filter,
                },
            )
            .execute()
        )
        rows = rpc.data or []

        # 3. Derive a snippet from chunk_text (first 300 chars, normalized whitespace).
        for row in rows:
            text = (row.get("chunk_text") or "").strip()
            snippet = " ".join(text.split())[:300]
            row["snippet"] = snippet

        # Cache stores JSON-serializable structures only; vectors are big and
        # not part of the user-visible payload, so we drop them.
        for row in rows:
            row.pop("embedding", None)

        return {"rows": rows, "fetched_at": time.time()}

    if use_cache:
        cached = cache.get_cache().get_cached(key, _fetch)
    else:
        cached = _fetch()
        cache.get_cache().put(key, cached)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return cached["rows"], elapsed_ms
