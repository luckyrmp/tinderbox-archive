"""Write enrichment results into tinderbox.enrichment + named_instances tables.

Idempotency: enrichment is append-only per the schema (latest = MAX(enriched_at)).
The worker decides whether to skip or re-enrich BEFORE calling these functions —
they always insert.
"""

import logging

from tinderbox import db
from tinderbox.enrichment.parser import EnrichmentResult, NamedInstanceDetection  # noqa: F401 (re-exported)

logger = logging.getLogger("tinderbox.enrichment.upsert")


def insert_enrichment(
    conversation_id: str,
    result: EnrichmentResult,
    enrichment_model: str,
    enrichment_version: int,
    extra_payload: dict | None = None,
) -> str:
    """Insert one enrichment row. Returns the new row id."""
    payload = {
        "raw_response": result.raw_response,
    }
    if extra_payload:
        payload.update(extra_payload)

    data = {
        "conversation_id": conversation_id,
        "summary": result.summary,
        "project_tags": result.project_tags,
        "client_tags": result.client_tags,
        "key_decisions": [kd.to_dict() for kd in result.key_decisions],
        "topics": result.topics,
        "enrichment_model": enrichment_model,
        "enrichment_version": enrichment_version,
        "payload": payload,
    }
    res = db.table("enrichment").insert(data).execute()
    return res.data[0]["id"]


def upsert_named_instances(
    conversation_id: str,
    detections: list[NamedInstanceDetection],
    first_seen_at: str | None = None,
) -> int:
    """For each detection: ensure named_instances row exists (by name),
    then upsert into the conversation_named_instances join table.

    Returns count of join rows written (i.e. detections processed).
    """
    if not detections:
        return 0

    # Pre-fetch existing named_instances by name to minimize RTTs.
    names = sorted({d.name for d in detections})
    existing_resp = (
        db.table("named_instances")
        .select("id,name")
        .in_("name", names)
        .execute()
    )
    name_to_id: dict[str, str] = {row["name"]: row["id"] for row in (existing_resp.data or [])}

    # Insert any missing names.
    for d in detections:
        if d.name in name_to_id:
            continue
        ins = (
            db.table("named_instances")
            .insert({
                "name": d.name,
                "first_seen_conversation_id": conversation_id,
                "first_seen_at": first_seen_at,
            })
            .execute()
        )
        if ins.data:
            name_to_id[d.name] = ins.data[0]["id"]
        else:
            # Race: someone else inserted between SELECT and INSERT. Re-fetch.
            refetch = (
                db.table("named_instances")
                .select("id")
                .eq("name", d.name)
                .limit(1)
                .execute()
            )
            if refetch.data:
                name_to_id[d.name] = refetch.data[0]["id"]
            else:
                logger.warning("named_instance insert lost race and re-fetch missed: %s", d.name)
                continue

    # Now upsert the join rows. PK is (conversation_id, named_instance_id).
    rows = []
    for d in detections:
        ni_id = name_to_id.get(d.name)
        if not ni_id:
            continue
        rows.append({
            "conversation_id": conversation_id,
            "named_instance_id": ni_id,
            "confidence": float(d.confidence),
            "evidence": d.evidence[:1000] if d.evidence else None,
        })

    if not rows:
        return 0

    (
        db.table("conversation_named_instances")
        .upsert(rows, on_conflict="conversation_id,named_instance_id")
        .execute()
    )
    return len(rows)


def insert_enrichment_failure(
    conversation_id: str,
    failure_reason: str,
    enrichment_model: str,
    enrichment_version: int,
    failure_payload: dict | None = None,
) -> str:
    """Insert a failure sentinel row for a conversation that could not be enriched.

    This makes failed conversations queryable:
        SELECT * FROM enrichment WHERE failure_reason IS NOT NULL
    and targetable for retry passes:
        tinderbox enrich --retry-failures
    """
    data = {
        "conversation_id": conversation_id,
        "failure_reason": failure_reason,
        "failure_payload": failure_payload or {},
        "enrichment_model": enrichment_model,
        "enrichment_version": enrichment_version,
    }
    res = db.table("enrichment").insert(data).execute()
    return res.data[0]["id"]


def already_enriched(
    conversation_id: str,
    enrichment_model: str,
    enrichment_version: int,
) -> bool:
    """Has this (conversation, model, version) tuple been enriched already (successfully)?

    A failure sentinel row (failure_reason IS NOT NULL) does NOT count as enriched —
    the worker will re-attempt it unless --reenrich is passed.
    """
    res = (
        db.table("enrichment")
        .select("id,failure_reason")
        .eq("conversation_id", conversation_id)
        .eq("enrichment_model", enrichment_model)
        .eq("enrichment_version", enrichment_version)
        .limit(1)
        .execute()
    )
    if not res.data:
        return False
    # A row with failure_reason set is a failure sentinel — not a successful enrichment.
    return res.data[0].get("failure_reason") is None
