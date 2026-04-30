"""Upsert helpers for writing parsed records to Supabase."""

import hashlib
import json
from typing import Any

from tinderbox import db
from tinderbox.ingest.retry import with_retry
from tinderbox.parser.conversation import ConversationRecord
from tinderbox.parser.message import MessageRecord
from tinderbox.parser.artifact import ArtifactRecord
from tinderbox.parser.attachment import AttachmentRecord


def _content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8-encoded text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def upsert_conversation(record: ConversationRecord, run_id: str,
                        run_started_at: str) -> str:
    """Upsert a conversation. Returns the conversation UUID (DB-side)."""
    data = {
        "export_id": record.export_id,
        "title": record.title,
        "created_at_source": record.created_at_source,
        "updated_at_source": record.updated_at_source,
        "raw_metadata": record.raw_metadata,
        "last_ingest_run_id": run_id,
        "last_seen_in_export_at": run_started_at,
        "privacy_class": "personal",
    }

    def _do():
        return (
            db.table("conversations")
            .upsert(data, on_conflict="export_id")
            .execute()
        )

    result = with_retry(_do, op_name=f"upsert conversation {record.export_id}")
    return result.data[0]["id"]


def get_conversation_id(export_id: str) -> str | None:
    """Look up the DB-side UUID for a conversation by its export_id."""
    result = (
        db.table("conversations")
        .select("id")
        .eq("export_id", export_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    return None


def insert_message(record: MessageRecord, conversation_id: str) -> tuple[str | None, bool]:
    """Insert a message if absent. Returns (message_id, was_inserted).

    `was_inserted` is True only when this call produced a new row. False when
    the row already existed (idempotent re-run). The runner uses this to
    distinguish messages_new from updated rows for accurate counters.
    """
    raw_content_payload: Any = record.raw_content
    # If truncated, prepend a metadata marker as the first block so the
    # truncation is visible to anything reading raw_content directly.
    if record.truncated and record.truncation_note:
        raw_content_payload = [
            {"_tinderbox_truncation_metadata": record.truncation_note},
            *record.raw_content,
        ]

    data = {
        "conversation_id": conversation_id,
        "export_id": record.export_id,
        "position": record.position,
        "role": record.role,
        "content": record.content,
        "raw_content": raw_content_payload,
        "created_at_source": record.created_at_source,
        "model": record.model,
        "stop_reason": record.stop_reason,
        "content_hash": _content_hash(record.content),
    }

    def _do():
        # ignore_duplicates=True: existing rows are not updated and NOT returned.
        # That's how we distinguish insert from no-op.
        return (
            db.table("messages")
            .upsert(data, on_conflict="conversation_id,position", ignore_duplicates=True)
            .execute()
        )

    result = with_retry(_do, op_name=f"insert message pos={record.position}")

    if result.data:
        return result.data[0]["id"], True

    # Conflict path: row already exists. Fetch its id so the caller can link
    # artifacts/attachments to the existing message.
    def _fetch_existing():
        return (
            db.table("messages")
            .select("id")
            .eq("conversation_id", conversation_id)
            .eq("position", record.position)
            .limit(1)
            .execute()
        )

    existing = with_retry(_fetch_existing, op_name=f"lookup existing msg pos={record.position}")
    if existing.data:
        return existing.data[0]["id"], False
    return None, False


def insert_artifact(record: ArtifactRecord, conversation_id: str,
                    message_id: str | None) -> bool:
    """Insert an artifact. Returns True if new, False if already existed."""
    data = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "artifact_id": record.artifact_id,
        "version": record.version,
        "command": record.command,
        "type": record.type,
        "language": record.language,
        "title": record.title,
        "content": record.content,
        "payload": record.payload,
        "created_at_source": record.created_at_source,
        "content_hash": _content_hash(record.content),
    }

    def _do():
        return db.table("artifacts").insert(data).execute()

    try:
        result = with_retry(
            _do, op_name=f"insert artifact {record.artifact_id}@v{record.version}"
        )
        return bool(result.data)
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower() or "23505" in str(e):
            return False
        raise


def insert_attachment(record: AttachmentRecord, conversation_id: str,
                      message_id: str | None) -> bool:
    """Insert an attachment. Returns True if new, False if already existed."""
    data = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "filename": record.filename,
        "mime_type": record.mime_type,
        "size_bytes": record.size_bytes,
        "file_hash": record.file_hash,
        "extracted_text": record.extracted_text,
        "payload": record.payload,
    }

    def _do():
        return db.table("attachments").insert(data).execute()

    try:
        result = with_retry(
            _do, op_name=f"insert attachment hash={record.file_hash[:8]}"
        )
        return bool(result.data)
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower() or "23505" in str(e):
            return False
        raise
