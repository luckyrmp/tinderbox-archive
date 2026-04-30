"""Typed error shapes for ingest_runs.errors JSONB array."""

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class IngestError:
    stage: str
    error_class: str
    message: str
    item_ref: dict[str, str] | None = None
    traceback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}
