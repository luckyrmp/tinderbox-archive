"""Parse a message from the export into a MessageRecord."""

import json
from dataclasses import dataclass, field
from typing import Any

from tinderbox.parser.render import render_blocks


# Postgres + PostgREST can't reliably insert rows with multi-MB JSONB columns
# within the default statement timeout. Cap raw_content at ~5 MB.
# When over the limit, replace the largest blocks with a truncation marker
# preserving their type and a hash for traceability.
RAW_CONTENT_BYTE_LIMIT = 5_000_000


@dataclass
class MessageRecord:
    export_id: str
    position: int
    role: str
    content: str
    raw_content: list[dict]
    created_at_source: str | None
    model: str | None = None
    stop_reason: str | None = None
    truncated: bool = False
    truncation_note: dict | None = None


def _block_size(block: dict) -> int:
    try:
        return len(json.dumps(block))
    except (TypeError, ValueError):
        return 0


def _truncate_oversized_blocks(blocks: list[dict]) -> tuple[list[dict], dict | None]:
    """If total serialized size exceeds the cap, replace the largest blocks with stubs.

    Returns (new_blocks, truncation_note or None).
    """
    sizes = [_block_size(b) for b in blocks]
    total = sum(sizes)
    if total <= RAW_CONTENT_BYTE_LIMIT:
        return blocks, None

    # Walk from largest to smallest, replacing until under the cap.
    indexed = sorted(enumerate(sizes), key=lambda x: -x[1])
    new_blocks = list(blocks)
    truncated_indices: list[dict] = []
    running = total

    for idx, sz in indexed:
        if running <= RAW_CONTENT_BYTE_LIMIT:
            break
        original = new_blocks[idx]
        stub = {
            "_truncated": True,
            "_original_type": original.get("type"),
            "_original_size_bytes": sz,
            "_reason": "raw_content exceeded RAW_CONTENT_BYTE_LIMIT",
        }
        # Preserve text field for text blocks (small) so render still works.
        if original.get("type") == "text" and isinstance(original.get("text"), str):
            stub["text"] = original["text"]
        new_blocks[idx] = stub
        truncated_indices.append({
            "block_index": idx,
            "original_type": original.get("type"),
            "original_size_bytes": sz,
        })
        running = running - sz + _block_size(stub)

    note = {
        "truncated_blocks": truncated_indices,
        "original_total_bytes": total,
        "new_total_bytes": running,
        "limit": RAW_CONTENT_BYTE_LIMIT,
    }
    return new_blocks, note


def parse_message(raw: dict, position: int) -> MessageRecord:
    """Parse one message dict from chat_messages array."""
    sender = raw.get("sender", "")
    if sender not in ("human", "assistant"):
        raise ValueError(f"Unexpected sender value: {sender!r} in message {raw.get('uuid', '?')}")

    blocks = raw.get("content", [])
    if not isinstance(blocks, list):
        blocks = []

    rendered = render_blocks(blocks)
    safe_blocks, truncation_note = _truncate_oversized_blocks(blocks)

    return MessageRecord(
        export_id=raw["uuid"],
        position=position,
        role=sender,
        content=rendered,
        raw_content=safe_blocks,
        created_at_source=raw.get("created_at"),
        truncated=truncation_note is not None,
        truncation_note=truncation_note,
    )
