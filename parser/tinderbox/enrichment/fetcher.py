"""Fetch a conversation in a form suitable for Haiku enrichment.

Strategy: pull all messages, render `[role@position]: content` blocks,
cap at MAX_CHARS. If the conversation exceeds the cap, truncate the
middle and leave a marker so Haiku knows.
"""

from dataclasses import dataclass

from tinderbox import db

# ~80K chars ≈ ~20K tokens. Comfortable for Haiku 4.5's 200K context
# while keeping cost reasonable.
MAX_CHARS = 80_000

# Per-message char cap so a single huge message can't blow the budget.
PER_MESSAGE_CHAR_CAP = 8_000

# When truncating, keep this many head + tail messages.
HEAD_TAIL_KEEP = 30


@dataclass
class ConversationForEnrichment:
    id: str
    export_id: str
    title: str | None
    created_at_source: str | None
    summary_at_export: str | None
    message_count: int
    rendered: str
    truncated: bool


def _render_message(role: str, position: int, content: str) -> str:
    if len(content) > PER_MESSAGE_CHAR_CAP:
        content = content[:PER_MESSAGE_CHAR_CAP] + " […message truncated…]"
    return f"[{role}@{position}]: {content}"


def fetch_for_enrichment(conversation: dict) -> ConversationForEnrichment:
    """Pull a conversation's messages and render for the Haiku prompt."""
    conv_id = conversation["id"]

    # Page through messages — postgrest caps at 1000 by default.
    all_msgs: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        page = (
            db.table("messages")
            .select("position,role,content")
            .eq("conversation_id", conv_id)
            .order("position")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = page.data or []
        all_msgs.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    rendered_msgs = [
        _render_message(m.get("role", "?"), m.get("position", 0), (m.get("content") or "").strip())
        for m in all_msgs
        if (m.get("content") or "").strip()
    ]

    # If small enough, just join everything.
    full = "\n\n".join(rendered_msgs)
    truncated = False
    if len(full) > MAX_CHARS and len(all_msgs) > 2 * HEAD_TAIL_KEEP:
        head = "\n\n".join(rendered_msgs[:HEAD_TAIL_KEEP])
        tail = "\n\n".join(rendered_msgs[-HEAD_TAIL_KEEP:])
        skipped = len(rendered_msgs) - 2 * HEAD_TAIL_KEEP
        full = (
            f"{head}\n\n"
            f"[…{skipped} messages omitted from the middle for length…]\n\n"
            f"{tail}"
        )
        truncated = True

    # Belt-and-suspenders: if even the head/tail render is too long, hard cap.
    if len(full) > MAX_CHARS:
        full = full[:MAX_CHARS] + "\n\n[…rendered content hard-capped at MAX_CHARS…]"
        truncated = True

    raw_meta = conversation.get("raw_metadata") or {}
    summary_at_export = raw_meta.get("summary_at_export") if isinstance(raw_meta, dict) else None

    return ConversationForEnrichment(
        id=conv_id,
        export_id=conversation["export_id"],
        title=conversation.get("title"),
        created_at_source=conversation.get("created_at_source"),
        summary_at_export=summary_at_export,
        message_count=len(all_msgs),
        rendered=full,
        truncated=truncated,
    )
