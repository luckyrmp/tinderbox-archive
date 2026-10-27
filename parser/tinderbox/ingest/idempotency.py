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


def release_failed_sha(sha256: str) -> int:
    """export_sha256 is UNIQUE, so a failed/orphaned run would block any retry of the
    same ZIP. Keep those rows for audit but suffix their sha so a new run can insert."""
    result = (
        db.table("ingest_runs")
        .select("id")
        .eq("export_sha256", sha256)
        .eq("status", "failed")
        .execute()
    )
    for row in (result.data or []):
        db.table("ingest_runs").update(
            {"export_sha256": f"{sha256}:failed:{row['id'][:8]}"}
        ).eq("id", row["id"]).execute()
    return len(result.data or [])
