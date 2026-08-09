"""Retry helper for transient Supabase / Cloudflare errors.

Cloudflare 520/521/522/524 and 5xx in general are transient. So are 408,
429 (rate-limited), and connection errors.
"""

import logging
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger("tinderbox.retry")

import re

# Phrases that strongly indicate transient infrastructure failures.
TRANSIENT_PHRASES = [
    "JSON could not be generated",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "Connection reset",
    "EOF occurred",
    "520: Web server",
    "521: Web server",
    "522: Connection",
    "524: A timeout",
    "Bad Gateway",
    "Service Unavailable",
    "Gateway Timeout",
    "statement timeout",
]

# Postgres SQLSTATE codes that mean "do not retry" (fail-fast).
# 23xxx: integrity constraint violations (unique, FK, check, not-null).
# 22xxx: data exception (cast/format errors).
# 42xxx: syntax/access (permission, undefined column, etc).
# 40001 / 40P01: serialization/deadlock — these ARE retryable but rare here.
NON_TRANSIENT_SQLSTATE = re.compile(
    r"['\"]code['\"]\s*:\s*['\"](23\d{3}|22\d{3}|42\d{3})"
)


def is_transient(exc: Exception) -> bool:
    """Best-effort classification — fail-fast on real errors, retry the rest."""
    msg = repr(exc)

    # Hard-stop: known non-transient SQLSTATE codes (e.g. 23505 duplicate key).
    if NON_TRANSIENT_SQLSTATE.search(msg):
        return False

    # Match transient phrases.
    return any(p in msg for p in TRANSIENT_PHRASES)


def with_retry(fn: Callable[[], T], *, max_attempts: int = 5,
               base_delay: float = 1.5, max_delay: float = 30.0,
               op_name: str = "operation") -> T:
    """Call fn with exponential-backoff retry on transient errors.

    Non-transient errors (e.g. constraint violations) raise immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not is_transient(e):
                raise
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = delay * 0.25 * random.random()
            sleep_for = delay + jitter
            logger.warning(
                "Transient error on %s (attempt %d/%d): %s — retrying in %.1fs",
                op_name, attempt, max_attempts, str(e)[:120], sleep_for,
            )
            time.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc
