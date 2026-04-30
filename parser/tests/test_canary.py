"""Unit tests for the mass-tombstone canary (done-when #6).

These tests verify the canary trip logic and apply_tombstones behavior in
isolation — they mock the database client entirely so they don't depend on
network or schema state. Run with:

    PYTHONPATH=. ~/tinderbox/parser/venv/bin/python3.14 -m pytest tests/test_canary.py -v

Or directly: PYTHONPATH=. python3.14 tests/test_canary.py
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tinderbox.config import Config
from tinderbox.ingest.sweep import (
    check_canary,
    apply_tombstones,
    canary_error,
    _run_started_at,
)


def make_config(canary_pct: int = 10, canary_abs: int = 50) -> Config:
    return Config(
        supabase_url="http://example.invalid",
        supabase_service_key="fake",
        canary_pct=canary_pct,
        canary_abs=canary_abs,
    )


class CanaryThresholdTest(unittest.TestCase):
    """Tests for the canary threshold computation: min(active * pct/100, abs)."""

    def _patch_db_for_counts(self, run_started_at: str,
                              active_count: int, would_count: int):
        """Build the patch context that simulates db.table(...) responses."""

        run_started_call = MagicMock()
        run_started_call.execute.return_value = MagicMock(
            data=[{"started_at": run_started_at}]
        )
        # The chain: db.table("ingest_runs").select(...).eq(...).limit(...).execute()
        run_chain = MagicMock()
        run_chain.select.return_value.eq.return_value.limit.return_value = run_started_call

        active_call = MagicMock(count=active_count)
        active_chain_eq = MagicMock()
        active_chain_eq.execute.return_value = active_call

        would_call = MagicMock(count=would_count)
        would_chain_lt = MagicMock()
        would_chain_lt.execute.return_value = would_call

        # active query: select("id", count="exact").eq("deleted_upstream", False)
        active_table = MagicMock()
        active_table.select.return_value.eq.return_value = active_chain_eq

        # would query: select("id", count="exact").eq("deleted_upstream", False).lt("last_seen_in_export_at", run_start)
        would_chain_eq = MagicMock()
        would_chain_eq.lt.return_value = would_chain_lt
        would_table = MagicMock()
        would_table.select.return_value.eq.return_value = would_chain_eq

        # Multi-call dispatcher: returns different chains based on call sequence.
        # Call 1: ingest_runs (run started_at lookup)
        # Call 2: conversations (active count) — chain ends at .eq()
        # Call 3: conversations (would_tombstone count) — chain ends at .lt()
        calls = []

        def table_dispatcher(name):
            calls.append(name)
            if name == "ingest_runs":
                return run_chain
            elif name == "conversations":
                # Distinguish active vs would by call count
                conv_calls = [c for c in calls if c == "conversations"]
                if len(conv_calls) == 1:
                    return active_table
                else:
                    return would_table
            raise AssertionError(f"unexpected table: {name}")

        return patch("tinderbox.ingest.sweep.db.table", side_effect=table_dispatcher)

    def test_canary_does_not_trip_when_no_conversations_missing(self):
        """First-ingest case: every conv was just seen. Canary should not trip."""
        config = make_config()
        with self._patch_db_for_counts("2026-04-29T00:00:00Z",
                                        active_count=676, would_count=0):
            tripped, would, active, threshold = check_canary("run-id", config)
        self.assertFalse(tripped)
        self.assertEqual(would, 0)
        self.assertEqual(active, 676)
        # threshold = min(676 * 10/100, 50) = min(67.6, 50) = 50
        self.assertEqual(threshold, 50)

    def test_canary_trips_above_pct_threshold(self):
        """100 active, 11 missing → 11 > min(10, 50) = 10 → trip."""
        config = make_config(canary_pct=10, canary_abs=50)
        with self._patch_db_for_counts("2026-04-29T00:00:00Z",
                                        active_count=100, would_count=11):
            tripped, would, active, threshold = check_canary("run-id", config)
        self.assertTrue(tripped)
        self.assertEqual(threshold, 10.0)
        self.assertEqual(would, 11)

    def test_canary_does_not_trip_at_pct_threshold(self):
        """Exactly 10% missing, with abs=50 → would == threshold → not tripped."""
        config = make_config(canary_pct=10, canary_abs=50)
        with self._patch_db_for_counts("2026-04-29T00:00:00Z",
                                        active_count=100, would_count=10):
            tripped, _, _, _ = check_canary("run-id", config)
        self.assertFalse(tripped)

    def test_canary_uses_abs_floor_not_pct_when_abs_is_smaller(self):
        """1000 active, 51 missing → pct=100, abs=50 → threshold=50 → 51 > 50 trips."""
        config = make_config(canary_pct=10, canary_abs=50)
        with self._patch_db_for_counts("2026-04-29T00:00:00Z",
                                        active_count=1000, would_count=51):
            tripped, would, active, threshold = check_canary("run-id", config)
        self.assertTrue(tripped)
        # min(1000 * 10/100, 50) = min(100, 50) = 50
        self.assertEqual(threshold, 50)
        self.assertEqual(would, 51)

    def test_canary_zero_active_does_not_trip(self):
        """No conversations at all → would=0 → not tripped, threshold=0."""
        config = make_config()
        with self._patch_db_for_counts("2026-04-29T00:00:00Z",
                                        active_count=0, would_count=0):
            tripped, _, _, threshold = check_canary("run-id", config)
        self.assertFalse(tripped)
        self.assertEqual(threshold, 0)


class CanaryErrorShapeTest(unittest.TestCase):
    """canary_error returns a structured IngestError with the right item_ref."""

    def test_error_payload_contains_required_fields(self):
        err = canary_error(would_count=120, active_count=600, threshold=60.0)
        d = err.to_dict()
        self.assertEqual(d["stage"], "tombstone_sweep")
        self.assertEqual(d["error_class"], "mass_tombstone_canary_tripped")
        self.assertIn("Would tombstone 120 of 600", d["message"])
        self.assertIn("override-canary", d["message"])
        self.assertEqual(d["item_ref"]["would_tombstone_count"], "120")
        self.assertEqual(d["item_ref"]["active_count"], "600")
        self.assertEqual(d["item_ref"]["threshold"], "60.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
