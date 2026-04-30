"""Extract artifacts from tool_use content blocks."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArtifactRecord:
    artifact_id: str
    version: int  # computed after collection, set by caller
    command: str
    type: str | None
    language: str | None
    title: str | None
    content: str
    created_at_source: str | None
    message_export_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawArtifactHit:
    """Intermediate: an artifact occurrence before version numbering."""
    artifact_id: str
    command: str
    type: str | None
    language: str | None
    title: str | None
    content: str
    created_at_source: str | None
    message_export_id: str
    block_index: int  # position within the message's content blocks
    payload: dict[str, Any] = field(default_factory=dict)


def extract_artifact_hits(blocks: list[dict], message_export_id: str,
                          message_created_at: str | None) -> list[RawArtifactHit]:
    """Pull artifact hits from a message's content blocks."""
    hits = []
    for block_idx, block in enumerate(blocks):
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != "artifacts":
            continue

        inp = block.get("input", {})
        command = inp.get("command", "")

        if command == "view":
            continue
        if command not in ("create", "update", "rewrite"):
            continue

        hits.append(RawArtifactHit(
            artifact_id=inp.get("id", ""),
            command=command,
            type=inp.get("type"),
            language=inp.get("language"),
            title=inp.get("title"),
            content=inp.get("content", ""),
            created_at_source=message_created_at,
            message_export_id=message_export_id,
            block_index=block_idx,
            payload={
                "version_uuid": inp.get("version_uuid"),
                "raw_input": inp,
            },
        ))
    return hits


def assign_versions(hits: list[RawArtifactHit]) -> list[ArtifactRecord]:
    """Assign monotonic version numbers per artifact_id.

    Hits must already be sorted by (message.created_at, block_index).
    Version starts at 1 for each artifact_id.
    """
    counters: dict[str, int] = {}
    records = []
    for hit in hits:
        aid = hit.artifact_id
        counters[aid] = counters.get(aid, 0) + 1
        records.append(ArtifactRecord(
            artifact_id=aid,
            version=counters[aid],
            command=hit.command,
            type=hit.type,
            language=hit.language,
            title=hit.title,
            content=hit.content,
            created_at_source=hit.created_at_source,
            message_export_id=hit.message_export_id,
            payload=hit.payload,
        ))
    return records
