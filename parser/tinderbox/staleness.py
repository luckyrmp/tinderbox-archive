"""Archive staleness check.

Cadence: launchd fires this DAILY at 09:00. Behavior:

  * Cooldown — never alert while still inside `cooldown_days` (default 7) of
    the most recent ingest. ("ticking a week since last import")
  * Stale — alert when days_since_last_ingest >= `stale_days` (default 30).
  * Debounce — don't re-alert within `debounce_hours` (default 22) of the
    previous alert. 22h not 24h so the daily 09:00 cron never just-misses.

State file: ~/tinderbox/state/staleness.json tracks last_alert_at.

Best-effort — never raises, never blocks anything.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tinderbox import db
from tinderbox.config import Config

logger = logging.getLogger("tinderbox.staleness")


DEFAULT_STALE_DAYS = 30
DEFAULT_COOLDOWN_DAYS = 7
DEFAULT_DEBOUNCE_HOURS = 22

STATE_PATH = Path.home() / "tinderbox" / "state" / "staleness.json"


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("staleness state unreadable, treating as empty: %s", e)
        return {}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _hours_since_last_alert(state: dict) -> float | None:
    raw = state.get("last_alert_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def days_since_last_ingest() -> float | None:
    """Returns float days since most recent ingest_runs row, or None if no runs."""
    res = (
        db.table("ingest_runs")
        .select("started_at")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    started = datetime.fromisoformat(res.data[0]["started_at"].replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - started
    return delta.total_seconds() / 86400


def latest_conversation_updated_at() -> datetime | None:
    """The most recent `updated_at_source` we have in the archive — i.e. the
    cutoff after which any new export content would be net-new."""
    res = (
        db.table("conversations")
        .select("updated_at_source")
        .not_.is_("updated_at_source", "null")
        .order("updated_at_source", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    raw = res.data[0]["updated_at_source"]
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def check(stale_days: int = DEFAULT_STALE_DAYS,
          cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
          debounce_hours: int = DEFAULT_DEBOUNCE_HOURS) -> tuple[bool, float | None, str]:
    """Returns (should_alert, days_since, message).

    `should_alert` already accounts for cooldown and debounce. Caller can post
    the message regardless of should_alert if they want the status text.
    """
    days = days_since_last_ingest()
    cutoff = latest_conversation_updated_at()
    cutoff_str = cutoff.date().isoformat() if cutoff else "(unknown)"

    state = _read_state()
    hours_since_alert = _hours_since_last_alert(state)

    re_export_advice = (
        f"\n\n  How to re-export:\n"
        f"  1. claude.ai → Settings → Account → Export Data\n"
        f"  2. claude.ai exports the full account (no date filter — that's a claude.ai\n"
        f"     limitation, not ours). The download will include everything since you\n"
        f"     started using Claude.\n"
        f"  3. Conversations with updated_at AFTER {cutoff_str} are the actual\n"
        f"     net-new content. Older conversations re-imported are no-ops via the\n"
        f"     parser's UUID + (conversation_id, position) idempotency — no\n"
        f"     duplicate writes, just the upsert path with no-op conflicts.\n"
        f"  4. Drop the ZIP into ~/tinderbox/inbox/ when it finishes downloading.\n"
        f"     Watcher (com.song.tinderbox.scan) picks it up within 15 min.\n"
        f"  5. Net-new messages auto-embed via the same path stage 2 uses.\n"
        f"\n"
        f"  Cost note: the download itself is wasteful (you're re-pulling old data\n"
        f"  every time), but Tinderbox only ingests deltas. If claude.ai ever adds\n"
        f"  a date-filter to export, that's the moment to revisit the workflow."
    )

    # Empty archive — alert (no cooldown can apply).
    if days is None:
        debounce_block = (
            hours_since_alert is not None and hours_since_alert < debounce_hours
        )
        should_alert = not debounce_block
        return should_alert, None, "no ingest_runs at all — archive is empty" + re_export_advice

    # Cooldown — recent import, stay silent regardless of stale_days threshold.
    if days < cooldown_days:
        return (
            False,
            days,
            f"archive is fresh — {days:.1f} days since last ingest, "
            f"inside {cooldown_days}-day cooldown. Latest conversation: {cutoff_str}."
        )

    # Between cooldown and stale_days threshold — fresh, don't alert.
    if days < stale_days:
        return (
            False,
            days,
            f"archive is fresh ({days:.1f} days since last ingest, "
            f"latest conversation updated {cutoff_str})"
        )

    # Stale. Decide whether to alert based on debounce.
    debounce_block = (
        hours_since_alert is not None and hours_since_alert < debounce_hours
    )
    should_alert = not debounce_block

    msg = (
        f"archive is {days:.1f} days stale (threshold {stale_days}).\n"
        f"  last ingest: {days:.1f} days ago\n"
        f"  last conversation updated_at_source: {cutoff_str}\n"
        f"  → conversations updated AFTER {cutoff_str} need a re-export to land in the archive."
    )
    if not should_alert:
        msg += f"\n  (debounced — last alert was {hours_since_alert:.1f}h ago)"
    msg += re_export_advice
    return should_alert, days, msg


def record_alert_sent() -> None:
    """Persist that we alerted right now, for the debounce window."""
    state = _read_state()
    state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
    _write_state(state)


def post_to_agent_comms(message: str) -> bool:
    """Best-effort post. Returns True on success, False otherwise."""
    try:
        # Use Supabase's agent_comms table directly. We're in tinderbox schema
        # by default, switch to public for this one query.
        client = db.get_client()
        client.schema("public").table("agent_comms").insert({
            "from_agent": "tinderbox",
            "to_agent": "reef",
            "message_type": "alert",
            "content": message,
            "metadata": {"source": "tinderbox.staleness"},
        }).execute()
        return True
    except Exception as e:
        logger.warning("agent_comms post failed (non-fatal): %s", e)
        return False
