"""Targeted recovery: re-process a single conversation from the archived ZIP.

Useful when run-level idempotency (sha256 dedup) prevents a full re-ingest
but a specific conversation needs reprocessing — e.g. after fixing a parser
bug that affected only some conversations.
"""

import json
import logging
import sys
import zipfile
from pathlib import Path

# Ensure parser package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from tinderbox.config import load_config
from tinderbox import db
from tinderbox.ingest.runner import _process_conversation, RunCounters
from tinderbox.ingest.errors import IngestError


def main():
    if len(sys.argv) != 3:
        print("Usage: recover_conv.py <zip_path> <conversation_export_id>")
        sys.exit(2)

    zip_path = sys.argv[1]
    target_uuid = sys.argv[2]

    config = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    )
    logger = logging.getLogger("tinderbox.recover")

    db.init(config)

    # Look up the most recent ingest_runs row for this ZIP
    import hashlib
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    sha256 = h.hexdigest()

    run_lookup = (
        db.table("ingest_runs")
        .select("id,started_at")
        .eq("export_sha256", sha256)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    if not run_lookup.data:
        logger.error("No ingest run found for sha256 %s", sha256)
        sys.exit(1)
    run_id = run_lookup.data[0]["id"]
    run_started_at = run_lookup.data[0]["started_at"]
    logger.info("Using existing run_id=%s (started_at=%s)", run_id, run_started_at)

    # Find the target conversation in the ZIP
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("conversations.json") as f:
            convos = json.load(f)
    target = next((c for c in convos if c.get("uuid") == target_uuid), None)
    if target is None:
        logger.error("Conversation %s not found in ZIP", target_uuid)
        sys.exit(1)

    logger.info("Processing conversation %s (%d messages)",
                target_uuid, len(target.get("chat_messages", [])))

    counters = RunCounters()
    errors: list[dict] = []

    try:
        _process_conversation(target, run_id, run_started_at, counters, errors)
    except Exception as e:
        logger.error("Conversation-level error: %s", e)
        sys.exit(1)

    logger.info(
        "Done. messages_new=%d artifacts_new=%d attachments_new=%d per_item_errors=%d",
        counters.messages_new, counters.artifacts_new, counters.attachments_new,
        len(errors),
    )

    if errors:
        for err in errors:
            logger.warning("  error: stage=%s class=%s ref=%s",
                           err.get("stage"), err.get("error_class"), err.get("item_ref"))


if __name__ == "__main__":
    main()
