"""Orchestrator: one ingest run from ZIP to DB."""

import json
import logging
import shutil
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from tinderbox import db
from tinderbox.config import Config
from tinderbox.ingest.errors import IngestError
from tinderbox.ingest.idempotency import compute_zip_sha256, zip_already_ingested, release_failed_sha
from tinderbox.ingest.upsert import (
    upsert_conversation, insert_message, insert_artifact, insert_attachment,
    get_conversation_id,
)
from tinderbox.ingest.sweep import check_canary, apply_tombstones, canary_error
from tinderbox.parser.streaming import stream_conversations, list_zip_contents
from tinderbox.parser.conversation import parse_conversation
from tinderbox.parser.message import parse_message
from tinderbox.parser.artifact import extract_artifact_hits, assign_versions
from tinderbox.parser.attachment import extract_attachments

logger = logging.getLogger("tinderbox.ingest")


class RunCounters:
    def __init__(self):
        self.conversations_seen = 0
        self.conversations_new = 0
        self.conversations_updated = 0
        self.messages_new = 0
        self.artifacts_new = 0
        self.attachments_new = 0


def run(zip_path: str, config: Config, canary_override: bool = False,
        override_run_id: str | None = None) -> str:
    """Execute a full ingest run. Returns the run_id."""

    zip_path = str(Path(zip_path).resolve())

    # Step 1: compute SHA-256
    sha256 = compute_zip_sha256(zip_path)
    logger.info("ZIP sha256: %s  path: %s", sha256, zip_path)

    # Step 2: check idempotency
    existing_run = zip_already_ingested(sha256)
    if existing_run:
        logger.info("Already ingested as run_id=%s — no-op.", existing_run)
        return existing_run

    # Step 3: orphan sweep — mark stale 'running' rows as failed
    _sweep_orphans(config)

    # Step 3b: free the unique sha from any earlier failed attempt of this same ZIP
    released = release_failed_sha(sha256)
    if released:
        logger.info("Released sha from %d earlier failed run(s) of this ZIP", released)

    # Step 4: create ingest_runs row
    now_iso = datetime.now(timezone.utc).isoformat()
    run_data = {
        "export_path": zip_path,
        "export_sha256": sha256,
        "status": "running",
    }
    result = db.table("ingest_runs").insert(run_data).execute()
    run_id = result.data[0]["id"]
    run_started_at = result.data[0]["started_at"]
    logger.info("Created ingest run: %s", run_id)

    counters = RunCounters()
    errors: list[dict] = []
    skipped: list[dict] = []

    # Step 5-6: open ZIP, stream conversations
    try:
        # Log skipped files
        zip_contents = list_zip_contents(zip_path)
        for fname in zip_contents:
            if fname == "conversations.json":
                continue
            elif fname.startswith("design_chats/"):
                skipped.append({"source": "design_chats", "file": fname, "reason": "deferred_to_stage_1_5"})
            elif fname == "memories.json":
                skipped.append({"source": "memories.json", "reason": "deferred_separate_pipeline"})
            elif fname == "projects.json":
                skipped.append({"source": "projects.json", "reason": "ingest_skipped_archived_for_enrichment"})
            elif fname == "users.json":
                skipped.append({"source": "users.json", "reason": "single_user_known"})
            else:
                skipped.append({"source": fname, "reason": "unrecognized_file"})

        # Step 7: process conversations
        for conv_raw in stream_conversations(zip_path):
            counters.conversations_seen += 1
            try:
                _process_conversation(conv_raw, run_id, run_started_at, counters, errors)
            except Exception as e:
                err = IngestError(
                    stage="parse_conversation",
                    error_class=type(e).__name__,
                    message=str(e),
                    item_ref={"conversation_export_id": conv_raw.get("uuid", "?")},
                    traceback=traceback.format_exc(),
                )
                errors.append(err.to_dict())
                logger.error("Error processing conversation %s: %s",
                             conv_raw.get("uuid", "?"), e)

            if counters.conversations_seen % 100 == 0:
                logger.info("Progress: %d conversations processed", counters.conversations_seen)

        # Step 8: tombstone sweep.
        # Invariant: sweep only runs when this run actually ingested ≥1 conversation
        # from a real ZIP.  apply_tombstones enforces this as a hard error so that
        # direct callers (tests, REPL, future scripts) cannot bypass it accidentally.
        if counters.conversations_seen == 0:
            logger.info("No conversations processed — skipping tombstone sweep.")
        else:
            if not canary_override:
                # Canary check MUST precede the UPDATE — apply_tombstones never runs
                # unless we reach the line below.
                tripped, would_count, active_count, threshold = check_canary(run_id, config)
                if tripped:
                    err = canary_error(would_count, active_count, threshold)
                    errors.append(err.to_dict())
                    logger.warning("Mass-tombstone canary tripped: %d of %d (threshold %.0f)",
                                   would_count, active_count, threshold)
                    _finalize_run(run_id, "failed", counters, errors, skipped,
                                  override_run_id=override_run_id)
                    return run_id
            else:
                logger.info("Canary override active — skipping canary check")

            tombstoned = apply_tombstones(run_id, conversations_ingested=counters.conversations_seen)
            if tombstoned:
                logger.info("Tombstoned %d conversations", tombstoned)

    except Exception as e:
        err = IngestError(
            stage="run_level",
            error_class=type(e).__name__,
            message=str(e),
            traceback=traceback.format_exc(),
        )
        errors.append(err.to_dict())
        logger.error("Run-level error: %s", e)
        _finalize_run(run_id, "failed", counters, errors, skipped)
        return run_id

    # Step 9-10: archive ZIP
    try:
        archive_dest = config.archive_dir / f"{sha256}.zip"
        config.archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(zip_path, str(archive_dest))
        logger.info("Archived ZIP to %s", archive_dest)
    except Exception as e:
        logger.warning("Failed to archive ZIP: %s (non-fatal)", e)

    # Determine final status
    status = "completed" if not errors else "partial"
    _finalize_run(run_id, status, counters, errors, skipped,
                  canary_override=canary_override, override_run_id=override_run_id)

    # Invalidate cached search/conversation responses — new content has landed.
    # Best-effort: failure here doesn't fail the run.
    if counters.conversations_new > 0 or counters.messages_new > 0 or counters.artifacts_new > 0:
        try:
            from tinderbox import cache
            n = cache.invalidate_searches()
            logger.info("Invalidated %d cached search entries due to new content", n)
        except Exception as e:
            logger.warning("Cache invalidation failed (non-fatal): %s", e)

    logger.info(
        "Run complete: status=%s convos=%d/%d msgs=%d artifacts=%d attachments=%d errors=%d",
        status, counters.conversations_new, counters.conversations_seen,
        counters.messages_new, counters.artifacts_new, counters.attachments_new,
        len(errors),
    )

    return run_id


def _process_conversation(conv_raw: dict, run_id: str, run_started_at: str,
                          counters: RunCounters,
                          errors: list[dict] | None = None):
    """Process one conversation: upsert convo, insert messages/artifacts/attachments.

    Per-message errors are caught and logged; one bad message does NOT kill the
    conversation. The conversation upsert itself, if it fails, raises (caller
    handles at conversation level).
    """

    conv_record = parse_conversation(conv_raw)

    # Check if conversation already exists to track new vs updated
    existing_id = get_conversation_id(conv_record.export_id)

    conv_id = upsert_conversation(conv_record, run_id, run_started_at)

    if existing_id is None:
        counters.conversations_new += 1
    else:
        counters.conversations_updated += 1

    # Sort messages by created_at, tie-break by uuid
    messages = conv_raw.get("chat_messages", [])
    messages.sort(key=lambda m: (m.get("created_at", ""), m.get("uuid", "")))

    # Collect artifact hits across all messages for version assignment
    all_artifact_hits = []

    # message_export_id -> db_message_id mapping for artifact linking
    msg_id_map: dict[str, str | None] = {}

    for position, msg_raw in enumerate(messages):
        msg_export_id = msg_raw.get("uuid", f"position-{position}")
        try:
            msg_record = parse_message(msg_raw, position)
        except ValueError as e:
            if errors is not None:
                errors.append(IngestError(
                    stage="parse_message",
                    error_class=type(e).__name__,
                    message=str(e),
                    item_ref={
                        "conversation_export_id": conv_record.export_id,
                        "message_export_id": msg_export_id,
                        "position": str(position),
                    },
                ).to_dict())
            logger.warning("Skipping message %s in conv %s: %s",
                           msg_export_id, conv_record.export_id, e)
            continue

        try:
            msg_db_id, was_inserted = insert_message(msg_record, conv_id)
            msg_id_map[msg_record.export_id] = msg_db_id

            if was_inserted:
                counters.messages_new += 1
        except Exception as e:
            if errors is not None:
                errors.append(IngestError(
                    stage="insert_message",
                    error_class=type(e).__name__,
                    message=str(e)[:500],
                    item_ref={
                        "conversation_export_id": conv_record.export_id,
                        "message_export_id": msg_export_id,
                        "position": str(position),
                        "content_length": str(len(msg_record.content)),
                        "block_count": str(len(msg_record.raw_content)),
                    },
                ).to_dict())
            logger.warning(
                "Skipping message pos=%d in conv %s (content_len=%d, blocks=%d): %s",
                position, conv_record.export_id,
                len(msg_record.content), len(msg_record.raw_content),
                str(e)[:120],
            )
            # Don't link artifacts/attachments to a non-existent message
            continue

        # Extract artifact hits from this message's blocks
        blocks = msg_raw.get("content", [])
        if isinstance(blocks, list):
            hits = extract_artifact_hits(
                blocks,
                message_export_id=msg_record.export_id,
                message_created_at=msg_raw.get("created_at"),
            )
            all_artifact_hits.extend(hits)

        # Extract attachments
        attachments = msg_raw.get("attachments", [])
        if attachments:
            for att_record in extract_attachments(attachments, msg_record.export_id):
                try:
                    new = insert_attachment(att_record, conv_id, msg_db_id)
                    if new:
                        counters.attachments_new += 1
                except Exception as e:
                    if errors is not None:
                        errors.append(IngestError(
                            stage="insert_attachment",
                            error_class=type(e).__name__,
                            message=str(e)[:500],
                            item_ref={
                                "conversation_export_id": conv_record.export_id,
                                "message_export_id": msg_record.export_id,
                                "filename": att_record.filename or "",
                                "file_hash": att_record.file_hash,
                            },
                        ).to_dict())
                    logger.warning("Skipping attachment %s in conv %s: %s",
                                   att_record.filename, conv_record.export_id, str(e)[:120])

    # Assign artifact versions and insert
    if all_artifact_hits:
        artifact_records = assign_versions(all_artifact_hits)
        for art in artifact_records:
            msg_db_id = msg_id_map.get(art.message_export_id)
            try:
                new = insert_artifact(art, conv_id, msg_db_id)
                if new:
                    counters.artifacts_new += 1
            except Exception as e:
                if errors is not None:
                    errors.append(IngestError(
                        stage="insert_artifact",
                        error_class=type(e).__name__,
                        message=str(e)[:500],
                        item_ref={
                            "conversation_export_id": conv_record.export_id,
                            "artifact_id": art.artifact_id,
                            "version": str(art.version),
                            "command": art.command,
                        },
                    ).to_dict())
                logger.warning("Skipping artifact %s@v%d in conv %s: %s",
                               art.artifact_id, art.version, conv_record.export_id,
                               str(e)[:120])


def _sweep_orphans(config: Config):
    """Mark stale 'running' ingest_runs as failed.

    Uses postgrest. The errors append is done client-side: fetch the row,
    append the orphan-sweep error, write back.
    """
    from datetime import datetime, timezone, timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=config.orphan_minutes)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        stale = (
            db.table("ingest_runs")
            .select("id,errors")
            .eq("status", "running")
            .lt("started_at", cutoff)
            .execute()
        )

        for row in (stale.data or []):
            existing_errors = row.get("errors") or []
            existing_errors.append({
                "stage": "orphan_sweep",
                "error_class": "OrphanRun",
                "message": "Marked as failed by orphan sweep",
            })
            db.table("ingest_runs").update({
                "status": "failed",
                "completed_at": now_iso,
                "errors": existing_errors,
            }).eq("id", row["id"]).execute()

        if stale.data:
            logger.info("Orphan sweep: marked %d stale runs as failed", len(stale.data))
    except Exception as e:
        logger.warning("Orphan sweep failed (non-fatal): %s", e)


def _finalize_run(run_id: str, status: str, counters: RunCounters,
                  errors: list[dict], skipped: list[dict] | None = None,
                  canary_override: bool = False,
                  override_run_id: str | None = None):
    """Update the ingest_runs row with final status and counters."""
    payload: dict = {}
    if skipped:
        payload["skipped"] = skipped
    if canary_override:
        payload["canary_override"] = True
    if override_run_id:
        payload["overrode_run_id"] = override_run_id

    data = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "conversations_seen": counters.conversations_seen,
        "conversations_new": counters.conversations_new,
        "conversations_updated": counters.conversations_updated,
        "messages_new": counters.messages_new,
        "artifacts_new": counters.artifacts_new,
        "attachments_new": counters.attachments_new,
        "errors": errors,
        "payload": payload,
    }

    db.table("ingest_runs").update(data).eq("id", run_id).execute()
