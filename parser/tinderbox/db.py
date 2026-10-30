"""Supabase client for Tinderbox. Uses service_role key (stage 1 expedient).

CRUD: supabase-py v2 with `.schema('tinderbox').table(...)` on every call.
The `tinderbox` schema must be in the project's PostgREST exposed schemas list
(Dashboard → API Settings → Data API → Exposed schemas).
"""

import httpx
from supabase import create_client, Client

from tinderbox.config import Config

_client: Client | None = None
_config: Config | None = None


def init(config: Config) -> Client:
    global _client, _config
    _config = config
    if config.db_dsn:
        from tinderbox.db_readonly import ReadOnlyClient
        _client = ReadOnlyClient(config.db_dsn)
    else:
        _client = create_client(config.supabase_url, config.supabase_service_key)
    return _client


def get_client() -> Client:
    if _client is None:
        raise RuntimeError("db.init() must be called before get_client()")
    return _client


def get_config() -> Config:
    if _config is None:
        raise RuntimeError("db.init() must be called before get_config()")
    return _config


def table(name: str):
    """Shortcut: client.schema(tinderbox).table(name) for every call."""
    return get_client().schema(get_config().schema).table(name)


def execute_sql(query: str, config: Config | None = None) -> list[dict]:
    """Execute raw SQL via Supabase's pg-meta REST endpoint.

    Used for tombstone sweep, canary check, and orphan sweep — operations
    that don't fit cleanly into postgrest.
    """
    cfg = config or get_config()
    url = f"{cfg.supabase_url}/pg/query"
    headers = {
        "apikey": cfg.supabase_service_key,
        "Authorization": f"Bearer {cfg.supabase_service_key}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json={"query": query}, headers=headers, timeout=60.0)
    if resp.status_code != 200:
        raise RuntimeError(f"SQL execution failed ({resp.status_code}): {resp.text}")
    return resp.json()
