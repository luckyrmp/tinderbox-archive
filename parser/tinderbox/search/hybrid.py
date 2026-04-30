"""Hybrid search: embed the query, call tinderbox.hybrid_search RPC, return rows."""

import time
from typing import Any

import httpx

from tinderbox import db
from tinderbox.config import Config
from tinderbox.embed.ollama_client import embed_batch


def hybrid_search(query_text: str, config: Config,
                  limit: int = 10,
                  cosine_filter: float = 0.6) -> tuple[list[dict[str, Any]], int]:
    """Run a hybrid search. Returns (results, latency_ms).

    Each result dict has the columns from tinderbox.hybrid_search RETURNS TABLE
    plus a derived `snippet` field built from chunk_text.
    """
    start = time.monotonic()

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

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return rows, elapsed_ms
