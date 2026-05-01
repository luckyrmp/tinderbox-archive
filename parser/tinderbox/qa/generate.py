"""Generate eval queries via Haiku from sampled conversations.

Three flavors per sample:
  - vague_semantic: "the conversation about X" — tests semantic recall.
  - specific_factual: "when did we decide Y" — tests precise recall.
  - fragmentary: short keyword-y phrasing — tests robustness to under-specified
    queries.

Frozen: queries are written once to tinderbox.eval_queries. Re-running
`tinderbox eval generate` regenerates ONLY for conversations not yet covered;
existing queries are never overwritten or deleted (set is_active=false to
retire one).
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from tinderbox import db
from tinderbox.config import Config
from tinderbox.qa.sample import (
    ConversationSample,
    fetch_excerpt,
    fetch_artifact_titles,
    sample_conversations,
)

logger = logging.getLogger("tinderbox.qa.generate")

GENERATION_MODEL = "claude-haiku-4-5"

PROMPT_TEMPLATE = """You are generating retrieval-eval queries for a personal claude.ai conversation archive owned by Lucky.

Below is one conversation from the archive. Your job: produce exactly THREE search queries that should retrieve THIS conversation when run against the archive's hybrid search (semantic + full-text).

The three queries must be in three distinct flavors:
1. VAGUE_SEMANTIC — natural-language description of the topic, no exact keywords. Example: "the conversation where we figured out the migration strategy"
2. SPECIFIC_FACTUAL — references a concrete fact, decision, name, or date present in the conversation. Example: "when did we lock in the 5MB raw_content limit"
3. FRAGMENTARY — short, keyword-y, the way someone might type into a search box mid-thought. 2-6 words. Example: "tinderbox truncation marker"

Strict rules:
- The conversation's TITLE is "{title}". Do NOT include the literal title or its exact phrasing in any query — that would be too easy a match. Reference the conversation's content instead.
- Each query must be answerable from THIS conversation's content (excerpt below). Do not invent facts.
- Each query should be distinctive enough to plausibly identify this conversation among ~676 in the archive.

Conversation context:
- Bucket: {bucket}
- Message count: {message_count}
- Has artifacts: {has_artifacts}
- Anthropic-generated summary (may be missing): {summary}
- Artifact titles (if any): {artifact_titles}

Excerpt:
{excerpt}

Respond ONLY with a JSON object exactly matching this schema, no markdown fences, no commentary:
{{"queries": [
  {{"flavor": "vague_semantic", "query": "..."}},
  {{"flavor": "specific_factual", "query": "..."}},
  {{"flavor": "fragmentary", "query": "..."}}
]}}
"""


@dataclass
class GeneratedQuery:
    flavor: str
    query_text: str


def _generate_for_sample(client: anthropic.Anthropic,
                         sample: ConversationSample,
                         excerpt: str,
                         artifact_titles: list[str]) -> list[GeneratedQuery]:
    prompt = PROMPT_TEMPLATE.format(
        title=sample.title or "(untitled)",
        bucket=sample.bucket,
        message_count=sample.message_count,
        has_artifacts=sample.has_artifacts,
        summary=(sample.summary_at_export or "")[:500],
        artifact_titles=", ".join(artifact_titles[:5]) if artifact_titles else "(none)",
        excerpt=excerpt or "(no excerpt available)",
    )

    resp = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text

    # Strip any stray markdown fences.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Bad JSON from Haiku for conv %s; raw=%r",
                       sample.export_id, text[:300])
        return []

    out: list[GeneratedQuery] = []
    for entry in parsed.get("queries", []):
        flavor = entry.get("flavor")
        query = (entry.get("query") or "").strip()
        if flavor in ("vague_semantic", "specific_factual", "fragmentary") and query:
            out.append(GeneratedQuery(flavor=flavor, query_text=query))
    return out


def generate_eval_set(config: Config, target_total: int = 50,
                      seed: int = 42) -> dict:
    """Generate eval queries for conversations not yet in eval_queries.

    Returns counters: {sampled, already_covered, queries_inserted, samples_skipped}.
    """
    load_dotenv(os.environ.get("TINDERBOX_ENV_FILE", str(Path.home() / ".secrets" / "tinderbox.env")))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)

    samples = sample_conversations(target_total=target_total, seed=seed)
    logger.info("Sampled %d conversations across buckets", len(samples))

    # Find which sample conversations already have queries — skip those.
    existing = (
        db.table("eval_queries")
        .select("source_conversation_id")
        .execute()
    )
    already = {r["source_conversation_id"] for r in (existing.data or [])}

    counters = {
        "sampled": len(samples),
        "already_covered": 0,
        "queries_inserted": 0,
        "samples_skipped": 0,
    }

    for sample in samples:
        if sample.id in already:
            counters["already_covered"] += 1
            continue

        excerpt = fetch_excerpt(sample.id)
        artifact_titles = fetch_artifact_titles(sample.id)

        try:
            queries = _generate_for_sample(client, sample, excerpt, artifact_titles)
        except Exception as e:
            logger.warning("Generation failed for conv %s: %s", sample.export_id, e)
            counters["samples_skipped"] += 1
            continue

        if not queries:
            counters["samples_skipped"] += 1
            continue

        rows = [
            {
                "query_text": q.query_text,
                "flavor": q.flavor,
                "source_conversation_id": sample.id,
                "generation_model": GENERATION_MODEL,
                "generation_payload": {
                    "bucket": sample.bucket,
                    "message_count": sample.message_count,
                    "has_artifacts": sample.has_artifacts,
                },
            }
            for q in queries
        ]
        db.table("eval_queries").insert(rows).execute()
        counters["queries_inserted"] += len(rows)
        logger.info("conv %s (%s, msgs=%d): %d queries",
                    sample.export_id[:8], sample.bucket,
                    sample.message_count, len(rows))

    return counters
