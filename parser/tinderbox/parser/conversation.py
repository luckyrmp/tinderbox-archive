"""Parse a conversation envelope from the export into a ConversationRecord."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ConversationRecord:
    export_id: str
    title: str | None
    created_at_source: str | None
    updated_at_source: str | None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def parse_conversation(raw: dict) -> ConversationRecord:
    """Parse one conversation dict from conversations.json."""
    raw_metadata: dict[str, Any] = {}

    # Preserve summary (Anthropic-generated, not our enrichment)
    if raw.get("summary"):
        raw_metadata["summary_at_export"] = raw["summary"]

    # Preserve account info
    account = raw.get("account")
    if isinstance(account, dict):
        raw_metadata["account_uuid"] = account.get("uuid")
    elif account is not None:
        raw_metadata["account"] = account

    # Capture any unexpected keys
    known_keys = {"uuid", "name", "summary", "created_at", "updated_at", "account", "chat_messages"}
    for k, v in raw.items():
        if k not in known_keys:
            raw_metadata[k] = v

    return ConversationRecord(
        export_id=raw["uuid"],
        title=raw.get("name"),
        created_at_source=raw.get("created_at"),
        updated_at_source=raw.get("updated_at"),
        raw_metadata=raw_metadata,
    )
