"""Idempotency checks: ZIP-level and conversation-level dedup."""

import hashlib
from pathlib import Path

from tinderbox import db


def compute_zip_sha256(zip_path: str | Path) -> str:
    """Compute SHA-256 of the ZIP file."""
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_already_ingested(sha256: str) -> str | None:
    """Check if this ZIP has already been ingested (non-failed run). Returns run_id if so, None otherwise."""
    result = (
        db.table("ingest_runs")
        .select("id")
        .eq("export_sha256", sha256)
        .neq("status", "failed")  # failed/orphaned runs must not block a retry or override-canary
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    return None
