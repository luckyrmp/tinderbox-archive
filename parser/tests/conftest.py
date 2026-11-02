"""Live-DB integration tests are opt-in.

test_modified_zip.py runs ingest_run(canary_override=True) against the
production tinderbox schema; its tombstone sweep marks every real conversation
deleted_upstream (happened 2026-11-01). test_reliability.py also hits live
services. Run them only with TINDERBOX_LIVE_TESTS=1.
"""
import os

collect_ignore = []
if os.environ.get("TINDERBOX_LIVE_TESTS") != "1":
    collect_ignore += ["test_modified_zip.py", "test_reliability.py"]
