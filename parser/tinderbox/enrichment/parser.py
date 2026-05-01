"""Parse Haiku's JSON enrichment response into a typed result."""

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NamedInstanceDetection:
    name: str
    confidence: float
    evidence: str


@dataclass
class KeyDecision:
    decision: str
    rationale: str

    def to_dict(self) -> dict:
        return {"decision": self.decision, "rationale": self.rationale}


@dataclass
class EnrichmentResult:
    summary: str
    project_tags: list[str] = field(default_factory=list)
    client_tags: list[str] = field(default_factory=list)
    key_decisions: list[KeyDecision] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    named_instances: list[NamedInstanceDetection] = field(default_factory=list)
    raw_response: str = ""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_haiku_response(text: str) -> EnrichmentResult:
    """Parse Haiku's JSON response. Tolerant to stray markdown fences but
    rejects malformed JSON loudly so the caller can log + skip."""
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Haiku response was not valid JSON: {e}. Raw: {cleaned[:500]}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Haiku response was not a JSON object. Got: {type(data).__name__}")

    summary = (data.get("summary") or "").strip()
    if not summary:
        raise ValueError("Haiku response missing or empty 'summary' field")

    project_tags = _normalize_str_list(data.get("project_tags"))
    client_tags = _normalize_str_list(data.get("client_tags"))
    topics = _normalize_str_list(data.get("topics"))

    key_decisions: list[KeyDecision] = []
    for kd in data.get("key_decisions") or []:
        if not isinstance(kd, dict):
            continue
        decision = (kd.get("decision") or "").strip()
        rationale = (kd.get("rationale") or "").strip()
        if decision:
            key_decisions.append(KeyDecision(decision=decision, rationale=rationale))

    named_instances: list[NamedInstanceDetection] = []
    for ni in data.get("named_instances") or []:
        if not isinstance(ni, dict):
            continue
        name = (ni.get("name") or "").strip()
        if not name:
            continue
        try:
            confidence = float(ni.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = (ni.get("evidence") or "").strip()
        # Belt-and-suspenders: prompt says >=0.7, but enforce here too.
        if confidence < 0.7:
            continue
        named_instances.append(NamedInstanceDetection(
            name=name, confidence=confidence, evidence=evidence,
        ))

    return EnrichmentResult(
        summary=summary,
        project_tags=project_tags,
        client_tags=client_tags,
        key_decisions=key_decisions,
        topics=topics,
        named_instances=named_instances,
        raw_response=cleaned,
    )


def _normalize_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in val:
        if not isinstance(item, str):
            continue
        s = item.strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
