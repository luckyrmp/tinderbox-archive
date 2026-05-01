"""Tinderbox cache adapter.

Wraps the canonical ~/shared/supabase_cache.py CachedSupabaseClient with
tinderbox-specific key helpers + a process-singleton accessor.

Design principle: cache results, not connections. The Supabase client itself
is owned by tinderbox/db.py. This module only holds the SQLite-backed read
mirror for query/result caching.

Cache key prefixes (configured in ~/shared/supabase_cache.py DEFAULT_TTLS):
  tinderbox_search:    — hybrid search results, 300s
  tinderbox_conv:      — get_conversation responses, 3600s
  tinderbox_enrichment: — enrichment lookups, 1800s
"""

import hashlib
import sys
from pathlib import Path

# The shared module lives at ~/shared/supabase_cache.py and isn't pip-installed;
# add it to the path explicitly.
_SHARED = str(Path.home() / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from supabase_cache import CachedSupabaseClient  # noqa: E402

_client: CachedSupabaseClient | None = None


def get_cache() -> CachedSupabaseClient:
    """Process-wide singleton CachedSupabaseClient, lazy-initialized."""
    global _client
    if _client is None:
        _client = CachedSupabaseClient()
    return _client


def search_key(query: str, limit: int, cosine_filter: float) -> str:
    """Stable key for a hybrid_search call. Hashes the query so length/special chars are safe."""
    h = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"tinderbox_search:{h}:l{limit}:c{cosine_filter}"


def conv_key(export_id: str, max_messages: int) -> str:
    """Stable key for a get_conversation call."""
    return f"tinderbox_conv:{export_id}:m{max_messages}"


def invalidate_searches() -> int:
    """Invalidate all cached search results. Called when content changes (ingest, enrichment)."""
    return get_cache().invalidate_prefix("tinderbox_search:")


def invalidate_conversation(export_id: str) -> int:
    """Invalidate all cached responses for one conversation."""
    return get_cache().invalidate_prefix(f"tinderbox_conv:{export_id}:")
