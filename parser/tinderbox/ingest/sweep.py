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


def apply_tombstones(run_id: str, conversations_ingested: int) -> int:
    """Apply tombstone flags to conversations not seen in this run.

    `conversations_ingested` must be the number of conversations the caller
    actually processed from a real ZIP in this run.  Callers MUST pass a
    positive value; passing 0 is a hard error — a run that ingested nothing
    has no business marking anything as deleted.

    Returns count of tombstoned conversations. Logs at WARNING when the count is
    large (>=50 or >=50% of active conversations) so that a future bulk-tombstone
    incident is loud in the parser.log even if it slipped past the canary
    (e.g. via canary_override or a misuse of this function from outside the
    runner).
    """
    if conversations_ingested < 1:
        raise ValueError(
            f"apply_tombstones called for run {run_id} with conversations_ingested="
            f"{conversations_ingested}. Refusing to tombstone: a run that processed "
            "no conversations from a real ZIP must not mark anything as deleted."
        )
    import logging
    sweep_logger = logging.getLogger("tinderbox.ingest.sweep")

    run_start = _run_started_at(run_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Pre-count for the warning threshold check.
    pre_count = (
        db.table("conversations")
        .select("id", count="exact")
        .eq("deleted_upstream", False)
        .execute()
    )
    active_before = pre_count.count or 0

    result = (
        db.table("conversations")
        .update({"deleted_upstream": True, "deleted_upstream_at": now_iso})
        .eq("deleted_upstream", False)
        .lt("last_seen_in_export_at", run_start)
        .execute()
    )
    n = len(result.data) if result.data else 0

    if n >= 50 or (active_before > 0 and n / active_before >= 0.5):
        sweep_logger.warning(
            "BULK TOMBSTONE APPLIED: run_id=%s tombstoned=%d / active_before=%d (%.1f%%). "
            "If this was unintended, conversations are recoverable via "
            "UPDATE tinderbox.conversations SET deleted_upstream=false, deleted_upstream_at=null "
            "WHERE deleted_upstream_at >= '%s'.",
            run_id, n, active_before,
            (100.0 * n / active_before) if active_before else 0.0,
            now_iso,
        )
    return n


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
