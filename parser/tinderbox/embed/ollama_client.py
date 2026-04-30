"""Ollama batch embedding client.

Uses POST /api/embed (the modern endpoint that accepts an `input` array),
not /api/embeddings (single-input legacy).

Defaults: 32 inputs per batch, 1024-dim output, 60s timeout per call.
"""

import logging
from typing import Iterable

import httpx

from tinderbox.config import Config

logger = logging.getLogger("tinderbox.embed.ollama")


class OllamaEmbeddingError(RuntimeError):
    pass


def embed_batch(texts: list[str], config: Config,
                client: httpx.Client | None = None) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order.

    Raises OllamaEmbeddingError on any failure — caller decides retry policy.
    """
    if not texts:
        return []

    payload = {"model": config.embedding_model, "input": texts}
    url = f"{config.ollama_url}/api/embed"

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=120.0)

    try:
        resp = client.post(url, json=payload)
        if resp.status_code != 200:
            raise OllamaEmbeddingError(
                f"Ollama returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
    finally:
        if own_client:
            client.close()

    embs = data.get("embeddings")
    if not isinstance(embs, list):
        raise OllamaEmbeddingError(
            f"Ollama response missing 'embeddings' array: {list(data.keys())}"
        )
    if len(embs) != len(texts):
        raise OllamaEmbeddingError(
            f"Ollama returned {len(embs)} embeddings for {len(texts)} inputs"
        )
    for i, e in enumerate(embs):
        if not isinstance(e, list) or len(e) != config.embedding_dim:
            raise OllamaEmbeddingError(
                f"Embedding {i} wrong shape: type={type(e).__name__} "
                f"len={len(e) if isinstance(e, list) else '?'} "
                f"expected dim={config.embedding_dim}"
            )

    return embs
