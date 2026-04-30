"""Stratified sampling for eval set generation.

Goal: pick ~50 conversations diverse enough that retrieval has to handle
multiple shapes. Buckets:
  - 15 long convos (>=20 messages) — tests "find the conversation" recall
  - 15 short convos (3-10 messages) — tests low-signal recall
  - 10 with artifacts — tests artifact-aware retrieval
  - 10 random across the rest — coverage filler

Within each bucket we order by `updated_at_source DESC` and take the first N.
This biases slightly toward recent conversations, which is fine for stage-1
data — the corpus is dense in the recent past anyway.
"""

import random
from dataclasses import dataclass

from tinderbox import db


@dataclass
class ConversationSample:
    id: str                 # DB uuid
    export_id: str
    title: str | None
    summary_at_export: str | None   # from raw_metadata
    message_count: int
    has_artifacts: bool
    bucket: str             # "long" | "short" | "with_artifacts" | "random"


def sample_conversations(target_total: int = 50,
                          seed: int = 42) -> list[ConversationSample]:
    """Return a stratified sample. Deterministic given the seed."""
    rng = random.Random(seed)

    # Pull conversation+message-count rollup. We do this by fetching messages
    # grouped by conversation_id; postgrest doesn't expose GROUP BY directly,
    # so we paginate everything client-side. The corpus is small (676 convos).

    convs_resp = (
        db.table("conversations")
        .select("id,export_id,title,raw_metadata,updated_at_source")
        .order("updated_at_source", desc=True)
        .execute()
    )
    all_convs: list[dict] = convs_resp.data or []

    # Message counts via separate select — pull all message rows and count
    # client-side. With 10,653 messages this is one paged scan.
    msg_counts: dict[str, int] = {}
    artifact_convs: set[str] = set()

    page_size = 1000
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            db.table("messages")
            .select("id,conversation_id")
            .gt("id", last_id)
            .order("id")
            .limit(page_size)
            .execute()
        )
        rows = page.data or []
        if not rows:
            break
        for row in rows:
            cid = row["conversation_id"]
            msg_counts[cid] = msg_counts.get(cid, 0) + 1
        last_id = rows[-1]["id"]

    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = (
            db.table("artifacts")
            .select("id,conversation_id")
            .gt("id", last_id)
            .order("id")
            .limit(page_size)
            .execute()
        )
        rows = page.data or []
        if not rows:
            break
        for row in rows:
            artifact_convs.add(row["conversation_id"])
        last_id = rows[-1]["id"]

    # Annotate
    annotated: list[ConversationSample] = []
    for c in all_convs:
        cid = c["id"]
        annotated.append(ConversationSample(
            id=cid,
            export_id=c["export_id"],
            title=c.get("title"),
            summary_at_export=(c.get("raw_metadata") or {}).get("summary_at_export"),
            message_count=msg_counts.get(cid, 0),
            has_artifacts=cid in artifact_convs,
            bucket="",
        ))

    # Bucket allocation. Targets are soft — if a bucket is short we backfill from random.
    targets = {
        "long": 15,
        "short": 15,
        "with_artifacts": 10,
        "random": 10,
    }
    if sum(targets.values()) != target_total:
        raise ValueError("bucket targets must sum to target_total")

    long_pool = [c for c in annotated if c.message_count >= 20]
    short_pool = [c for c in annotated if 3 <= c.message_count <= 10]
    artifact_pool = [c for c in annotated if c.has_artifacts]
    rng.shuffle(long_pool)
    rng.shuffle(short_pool)
    rng.shuffle(artifact_pool)

    picked: dict[str, ConversationSample] = {}

    def take(pool: list[ConversationSample], bucket: str, n: int):
        for c in pool:
            if c.id in picked:
                continue
            picked[c.id] = ConversationSample(**{**c.__dict__, "bucket": bucket})
            if len([p for p in picked.values() if p.bucket == bucket]) >= n:
                break

    take(long_pool, "long", targets["long"])
    take(short_pool, "short", targets["short"])
    take(artifact_pool, "with_artifacts", targets["with_artifacts"])

    # Random filler from anything not yet picked.
    remaining = [c for c in annotated if c.id not in picked]
    rng.shuffle(remaining)
    for c in remaining[:targets["random"]]:
        picked[c.id] = ConversationSample(**{**c.__dict__, "bucket": "random"})

    # If we under-filled any bucket, top off from random pool.
    deficit = target_total - len(picked)
    if deficit > 0:
        for c in remaining[targets["random"]:]:
            if c.id in picked:
                continue
            picked[c.id] = ConversationSample(**{**c.__dict__, "bucket": "random"})
            deficit -= 1
            if deficit <= 0:
                break

    return list(picked.values())


def fetch_excerpt(conversation_id: str, max_chars: int = 800) -> str:
    """Pull a representative excerpt: the first non-empty message's content,
    capped at max_chars. Used as Haiku context when generating queries."""
    res = (
        db.table("messages")
        .select("content,role,position")
        .eq("conversation_id", conversation_id)
        .order("position")
        .limit(20)
        .execute()
    )
    rows = res.data or []
    parts = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"[{r.get('role')}@{r.get('position')}]: {content[:max_chars]}")
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars * 2]


def fetch_artifact_titles(conversation_id: str) -> list[str]:
    res = (
        db.table("artifacts")
        .select("title")
        .eq("conversation_id", conversation_id)
        .execute()
    )
    return [r["title"] for r in (res.data or []) if r.get("title")]
