"""Tombstone sweep and mass-tombstone canary.

Uses postgrest filters (no raw SQL) so it works against Supabase REST.
"""

from datetime import datetime, timezone

from tinderbox import db
from tinderbox.config import Config
from tinderbox.ingest.errors import IngestError


def _run_started_at(run_id: str) -> str:
    result = (
        db.table("ingest_runs")
        .select("started_at")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise RuntimeError(f"Run not found: {run_id}")
    return result.data[0]["started_at"]


def check_canary(run_id: str, config: Config) -> tuple[bool, int, int, float]:
    """Check if tombstone sweep would trip the canary.

    Returns (tripped, would_tombstone_count, active_count, threshold).
    """
    run_start = _run_started_at(run_id)

    active_result = (
        db.table("conversations")
        .select("id", count="exact")
        .eq("deleted_upstream", False)
        .execute()
    )
    active_count = active_result.count or 0

    would_result = (
        db.table("conversations")
        .select("id", count="exact")
        .eq("deleted_upstream", False)
        .lt("last_seen_in_export_at", run_start)
        .execute()
    )
    would_count = would_result.count or 0

    threshold = min(active_count * config.canary_pct / 100.0, config.canary_abs)

    tripped = would_count > threshold
    return tripped, would_count, active_count, threshold


def apply_tombstones(run_id: str) -> int:
    """Apply tombstone flags to conversations not seen in this run.

    Returns count of tombstoned conversations.
    """
    run_start = _run_started_at(run_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    result = (
        db.table("conversations")
        .update({"deleted_upstream": True, "deleted_upstream_at": now_iso})
        .eq("deleted_upstream", False)
        .lt("last_seen_in_export_at", run_start)
        .execute()
    )

    return len(result.data) if result.data else 0


def canary_error(would_count: int, active_count: int, threshold: float) -> IngestError:
    """Create the canary-tripped error object."""
    return IngestError(
        stage="tombstone_sweep",
        error_class="mass_tombstone_canary_tripped",
        message=(
            f"Would tombstone {would_count} of {active_count} active conversations "
            f"(threshold: {threshold:.0f}). Run halted. Use "
            f"'tinderbox override-canary <run_id>' to force."
        ),
        item_ref={
            "would_tombstone_count": str(would_count),
            "active_count": str(active_count),
            "threshold": str(threshold),
        },
    )
