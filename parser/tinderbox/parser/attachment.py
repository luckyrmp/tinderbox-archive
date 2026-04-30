"""Extract attachments from message attachment arrays."""

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AttachmentRecord:
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    file_hash: str
    extracted_text: str | None
    message_export_id: str
    payload: dict[str, Any] = field(default_factory=dict)


def extract_attachments(attachments: list[dict], message_export_id: str) -> list[AttachmentRecord]:
    """Parse attachment dicts from a message's attachments array."""
    records = []
    for att in attachments:
        extracted = att.get("extracted_content") or ""
        file_hash = hashlib.sha256(extracted.encode("utf-8")).hexdigest()

        records.append(AttachmentRecord(
            filename=att.get("file_name"),
            mime_type=att.get("file_type"),
            size_bytes=att.get("file_size"),
            file_hash=file_hash,
            extracted_text=extracted or None,
            message_export_id=message_export_id,
            payload={"raw": att},
        ))
    return records
