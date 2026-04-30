"""Run frozen retrieval-QA queries against the live hybrid_search and score hits."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tinderbox import db
from tinderbox.config import Config
from tinderbox.search.hybrid import hybrid_search

logger = logging.getLogger("tinderbox.qa.runner")


@dataclass
class PerQueryResult:
    eval_query_id: str
    query_text: str
    flavor: str
    source_conversation_id: str
    returned_conversation_ids: list[str]
    hit_position: int | None
    latency_ms: int
    top_score: float | None


@dataclass
class QaCounters:
    queries_run: int = 0
    hit_at_1: int = 0
    hit_at_3: int = 0
    hit_at_10: int = 0
    failures: int = 0
    per_query: list[PerQueryResult] = field(default_factory=list)


def run_qa(config: Config, limit: int | None = None,
           cosine_filter: float = 0.6,
           search_limit: int = 10) -> str:
    """Execute every active eval query, score hits, persist eval_runs row.
    Returns the eval_run id."""

    queries_resp = (
        db.table("eval_queries")
        .select("id,query_text,flavor,source_conversation_id")
        .eq("is_active", True)
        .order("created_at")
        .execute()
    )
    queries = queries_resp.data or []
    if limit is not None:
        queries = queries[:limit]
    if not queries:
        raise RuntimeError("No active eval_queries to run. Generate them first.")

    started_at = datetime.now(timezone.utc).isoformat()

    run_insert = (
        db.table("eval_runs")
        .insert({
            "started_at": started_at,
            "config_snapshot": {
                "cosine_filter": cosine_filter,
                "search_limit": search_limit,
                "embedding_model": config.embedding_model,
            },
        })
        .execute()
    )
    run_id = run_insert.data[0]["id"]

    counters = QaCounters()

    for q in queries:
        try:
            results, latency_ms = hybrid_search(
                q["query_text"], config,
                limit=search_limit, cosine_filter=cosine_filter,
            )
        except Exception as e:
            logger.warning("Query failed: %s -- %s", q["query_text"][:80], e)
            counters.failures += 1
            continue

        returned_convs: list[str] = []
        seen = set()
        for r in results:
            cid = r.get("conversation_id")
            if cid and cid not in seen:
                returned_convs.append(cid)
                seen.add(cid)

        hit_pos: int | None = None
        for i, cid in enumerate(returned_convs, 1):
            if cid == q["source_conversation_id"]:
                hit_pos = i
                break

        if hit_pos == 1:
            counters.hit_at_1 += 1
        if hit_pos and hit_pos <= 3:
            counters.hit_at_3 += 1
        if hit_pos and hit_pos <= 10:
            counters.hit_at_10 += 1

        counters.queries_run += 1
        counters.per_query.append(PerQueryResult(
            eval_query_id=q["id"],
            query_text=q["query_text"],
            flavor=q["flavor"],
            source_conversation_id=q["source_conversation_id"],
            returned_conversation_ids=returned_convs,
            hit_position=hit_pos,
            latency_ms=latency_ms,
            top_score=results[0].get("combined_score") if results else None,
        ))

    completed_at = datetime.now(timezone.utc).isoformat()
    db.table("eval_runs").update({
        "completed_at": completed_at,
        "queries_run": counters.queries_run,
        "hit_at_1": counters.hit_at_1,
        "hit_at_3": counters.hit_at_3,
        "hit_at_10": counters.hit_at_10,
        "per_query_results": [pr.__dict__ for pr in counters.per_query],
        "notes": f"failures={counters.failures}",
    }).eq("id", run_id).execute()

    return run_id
