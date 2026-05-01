"""Stage 4 enrichment worker — call Haiku per conversation, write structured
annotations into tinderbox.enrichment + tinderbox.named_instances.
"""

import logging
import os
import time
from dataclasses import dataclass, field

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from tinderbox import db
from tinderbox.config import Config
from tinderbox.enrichment.fetcher import fetch_for_enrichment
from tinderbox.enrichment.parser import parse_haiku_response
from tinderbox.enrichment.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from tinderbox.enrichment.upsert import (
    already_enriched,
    insert_enrichment,
    insert_enrichment_failure,
    upsert_named_instances,
)

logger = logging.getLogger("tinderbox.enrichment")

ENRICHMENT_MODEL = "claude-haiku-4-5"
ENRICHMENT_VERSION = 1

MAX_OUTPUT_TOKENS = 3000
RETRY_ATTEMPTS = 3


@dataclass
class EnrichmentCounters:
    seen: int = 0
    enriched: int = 0
    skipped_already: int = 0
    skipped_empty: int = 0
    errors: int = 0
    error_details: list[dict] = field(default_factory=list)
    named_instance_links: int = 0
    failure_rows_written: int = 0
    truncated_for_prompt: int = 0


@retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    )),
    reraise=True,
)
def _call_haiku(client: anthropic.Anthropic, conv_for_prompt,
                max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Single Haiku call for one conversation. Returns the raw text response."""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=conv_for_prompt.title or "(untitled)",
        created_at=conv_for_prompt.created_at_source or "(unknown)",
        summary_at_export=(conv_for_prompt.summary_at_export or "(none)")[:1000],
        rendered=conv_for_prompt.rendered,
    )

    # Prefill the assistant turn with `{` so Haiku cannot drift into prose.
    # The opening brace is part of the response we'll concatenate back.
    resp = client.messages.create(
        model=ENRICHMENT_MODEL,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "{"},
        ],
    )

    text = "{"
    for block in resp.content:
        if hasattr(block, "text"):
            text += block.text
    return text


def fetch_conversations_to_enrich(
    limit: int | None,
    only_conv_ids: list[str] | None,
    reenrich: bool,
) -> list[dict]:
    """Pull the candidate conversation list (paged)."""
    all_convs: list[dict] = []
    page_size = 1000
    offset = 0

    while True:
        q = (
            db.table("conversations")
            .select("id,export_id,title,created_at_source,raw_metadata,deleted_upstream")
            .order("updated_at_source", desc=True)
            .range(offset, offset + page_size - 1)
        )
        if only_conv_ids:
            q = q.in_("id", only_conv_ids)
        page = q.execute()
        rows = page.data or []
        all_convs.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    # Default: skip tombstoned conversations (still memorial-readable but
    # not worth re-enriching). Override with --include-deleted via caller.
    all_convs = [c for c in all_convs if not c.get("deleted_upstream")]

    if limit is not None:
        all_convs = all_convs[:limit]

    return all_convs


def run_enrichment(
    config: Config,
    limit: int | None = None,
    only_conv_ids: list[str] | None = None,
    reenrich: bool = False,
    retry_failures: bool = False,
    sleep_between_ms: int = 0,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> EnrichmentCounters:
    """Main loop. Returns counters for the run."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)

    convs = fetch_conversations_to_enrich(limit, only_conv_ids, reenrich)
    counters = EnrichmentCounters()
    total = len(convs)
    logger.info("Enrichment pass starting: %d conversation(s) to process", total)

    for i, conv in enumerate(convs, 1):
        counters.seen += 1
        conv_id = conv["id"]
        export_id = conv["export_id"]

        try:
            if not reenrich:
                res = (
                    db.table("enrichment")
                    .select("id,failure_reason")
                    .eq("conversation_id", conv_id)
                    .eq("enrichment_model", ENRICHMENT_MODEL)
                    .eq("enrichment_version", ENRICHMENT_VERSION)
                    .limit(1)
                    .execute()
                )
                row = res.data[0] if res.data else None
                if retry_failures:
                    # Only process rows that exist AND have a failure_reason set.
                    if not row or row.get("failure_reason") is None:
                        counters.skipped_already += 1
                        continue
                else:
                    # Normal path: skip if successfully enriched (row exists with no failure_reason).
                    if row and row.get("failure_reason") is None:
                        counters.skipped_already += 1
                        continue

            for_prompt = fetch_for_enrichment(conv)

            if for_prompt.message_count == 0 or not for_prompt.rendered.strip():
                counters.skipped_empty += 1
                logger.info("Skipping empty conversation %s (%s)", export_id, conv.get("title"))
                continue

            if for_prompt.truncated:
                counters.truncated_for_prompt += 1

            raw_text = _call_haiku(client, for_prompt, max_tokens=max_tokens)
            result = parse_haiku_response(raw_text)

            extra_payload = {
                "title_at_enrichment": for_prompt.title,
                "message_count_at_enrichment": for_prompt.message_count,
                "prompt_truncated": for_prompt.truncated,
            }

            insert_enrichment(
                conversation_id=conv_id,
                result=result,
                enrichment_model=ENRICHMENT_MODEL,
                enrichment_version=ENRICHMENT_VERSION,
                extra_payload=extra_payload,
            )

            n_links = upsert_named_instances(
                conversation_id=conv_id,
                detections=result.named_instances,
                first_seen_at=conv.get("created_at_source"),
            )
            counters.named_instance_links += n_links
            counters.enriched += 1

            if i % 10 == 0 or i == total:
                logger.info(
                    "Progress %d/%d: enriched=%d skipped_already=%d skipped_empty=%d errors=%d",
                    i, total,
                    counters.enriched, counters.skipped_already,
                    counters.skipped_empty, counters.errors,
                )

            if sleep_between_ms:
                time.sleep(sleep_between_ms / 1000.0)

        except Exception as e:
            counters.errors += 1
            err_info = {
                "conversation_id": conv_id,
                "export_id": export_id,
                "title": conv.get("title"),
                "error_class": type(e).__name__,
                "message": str(e)[:1000],
            }
            counters.error_details.append(err_info)
            logger.error("Error enriching %s (%s): %s: %s",
                         export_id, conv.get("title"), type(e).__name__, str(e)[:300])
            # Write a failure sentinel so this conversation is queryable and retryable.
            try:
                failure_reason = (
                    "haiku_json_parse_failed" if "parse" in type(e).__name__.lower() or "json" in str(e).lower()
                    else "rate_limit_exhausted" if isinstance(e, Exception) and "rate" in str(e).lower()
                    else "api_error" if "anthropic" in type(e).__module__.lower()
                    else "unknown"
                )
                insert_enrichment_failure(
                    conversation_id=conv_id,
                    failure_reason=failure_reason,
                    enrichment_model=ENRICHMENT_MODEL,
                    enrichment_version=ENRICHMENT_VERSION,
                    failure_payload={"error_class": type(e).__name__, "message": str(e)[:2000]},
                )
                counters.failure_rows_written += 1
            except Exception as fe:
                logger.warning("Could not write failure row for %s: %s", export_id, fe)

    logger.info(
        "Enrichment pass complete: enriched=%d skipped_already=%d skipped_empty=%d "
        "errors=%d failure_rows=%d truncated=%d named_instance_links=%d",
        counters.enriched, counters.skipped_already, counters.skipped_empty,
        counters.errors, counters.failure_rows_written,
        counters.truncated_for_prompt, counters.named_instance_links,
    )

    # Best-effort: invalidate search cache when enrichment lands new annotations.
    # Hybrid search results don't currently include enrichment fields, but they
    # will once stage 4 is wired into hybrid_search ranking. Invalidating now
    # keeps the cache from going stale across that future change.
    if counters.enriched > 0:
        try:
            from tinderbox import cache
            n = cache.invalidate_searches() or 0
            logger.info("Invalidated %d cached search entries due to new enrichment", n)
        except Exception as e:
            logger.warning("Cache invalidation failed (non-fatal): %s", e)

    return counters
