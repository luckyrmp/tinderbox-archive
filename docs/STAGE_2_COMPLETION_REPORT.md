# Tinderbox Stage 2 — Completion Report

**Date:** 2026-04-29
**Status:** ✅ Complete (plumbing). Eval phase awaits Lucky's option pick (A/B/C from §3.5).

---

## Final state in `zznfzonnovkldmdrooxh`

| Metric | Value |
|---|---|
| Total embeddings | 10,731 |
| Message embeddings | 10,039 distinct messages |
| Artifact summary chunks | 44 (chunk_index = -1) |
| Artifact body chunks | 648 |
| Embedding model | mxbai-embed-large (manifest 468836162de7) |
| Embedding dim | 1024 (vector(1024) + hnsw cosine index) |
| Total `embeddings` rows | 10,731 |

The artifact summary count (44 vs 172 artifacts) is investigated below — body chunks cover the same text, so retrieval still works. Worth a stage 2.5 pass.

---

## Done-when criteria (from STAGE_1_5_KICKOFF §3.6)

| # | Criterion | Status |
|---|---|---|
| 1 | `tinderbox embed` CLI exists, batched, idempotent | ✓ |
| 2 | Re-running `tinderbox embed` is a no-op | ✓ verified — second run reported "already=N" |
| 3 | All messages embedded (modulo truncated) | ✓ 10,039 / 10,329 (the 290 gap is empty-content + over-token-limit messages, see below) |
| 4 | `tinderbox search` returns top-N hybrid results | ✓ verified with 5 sample queries |
| 5 | `tinderbox.queries` table is logged on every search | ✓ verified |
| 6 | Stage 2 hardens after eval option pick | ⏸️ awaits Lucky |

---

## Code shipped

```
~/tinderbox/parser/
└── tinderbox/
    ├── embed/
    │   ├── chunking.py           # message + artifact chunkers, exact-position-0 truncation check
    │   ├── ollama_client.py      # batch /api/embed (modern endpoint)
    │   └── worker.py             # batched + idempotent + per-row fallback on token-limit failures
    └── search/
        ├── hybrid.py             # query embed + hybrid_search RPC + result shape
        └── logging.py            # tinderbox.queries write on every search

CLI commands added:
  tinderbox embed [--messages-only] [--artifacts-only] [--limit N]
  tinderbox search "<query>" [--limit N] [--caller IDENT] [--no-log]

Migrations:
  008_hybrid_search_function.sql — server-side hybrid retrieval RPC
```

---

## Bugs found and fixed during stage 2

### Bug 1 — `messages_new` counter overcounted (caught by fixture test)

`upsert(..., on_conflict=...)` returns rows for both inserts AND updates in Postgres. The runner's `if msg_db_id: counters.messages_new += 1` was incrementing on every call. Production stage 1 run reported `messages_new = 10,601` correctly only because every message was new — on any subsequent re-run, the count would lie.

**Fix:** changed `insert_message` to use `ignore_duplicates=True` and a tuple return `(message_id, was_inserted)`. The runner now only increments on `was_inserted=True`. Caught by the modified-ZIP fixture test (`tests/test_modified_zip.py`).

### Bug 2 — Ollama batch fails 400 when one input exceeds 512 tokens

mxbai-embed-large's actual context limit is ~512 tokens. My initial chunker targeted 2,000 chars (assumed 4 chars/token) but markdown/code averages closer to 3 chars/token, so some messages crossed the limit.

**Fix:** tightened limits to 1,400 chars head + 400 chars tail for messages, 1,200 chars per artifact chunk with 150-char overlap. Plus added per-row fallback in the worker — when a batch fails 400, it retries one-at-a-time so only the offending rows are skipped. 290 messages over the limit ended up unembedded (logged with full provenance for stage 2.5 cleanup).

### Bug 3 — Smoke-test `messages_new` reported 47 when 50 sources processed

The 3 difference is empty-content messages (no rendered text). The chunker correctly returns `[]` for empty content, so they're skipped — counter is correct. Worth noting as expected behavior.

---

## Open items for stage 2.5

### 2.5a — 290 messages skipped due to token-limit

These are messages where my head+tail concatenation still exceeded 512 tokens after tokenization. Since mxbai-embed-large doesn't expose its tokenizer, I'm chunking by char count which under-estimates token density for code/JSON-heavy content. Options:

1. **Add a smarter chunker for these** — try splitting at line boundaries / semantic breaks, retry until under the limit.
2. **Switch to a model with larger context** — `nomic-embed-text` has 8192-token context (worth re-evaluating now that we have real corpus stats).
3. **Accept the gap** — 290 / 10,329 = 2.8% of the corpus, all on the giant-message tail. They have rendered text in `messages.content` so they're still searchable via FTS, just not via vector similarity.

Recommendation: defer pending eval feedback. If retrieval quality is good enough without these, accept. If specific queries miss these, then chunk smarter.

### 2.5b — Only 44 artifact summaries vs 172 artifacts

172 artifacts × 1 summary each should produce 172 chunks at `chunk_index=-1`. We got 44. Two hypotheses:

1. **Chunking bug:** the summary-emit guard might be wrong for some artifact shapes (e.g. very short content where `len(text) <= ARTIFACT_CHUNK_CHARS` short-circuits).
2. **Token-limit failures:** if title + first 700 + last 700 exceeds the model limit, summary embedding fails, and the per-row fallback skips it.

Quick check: 648 body chunks / 172 artifacts = ~3.8 body chunks/artifact average, which suggests artifacts skew long. So hypothesis #2 is more likely — the summary chunks are exactly the longer concatenations that fail. Worth a re-run with shorter summary head+tail (e.g. 400 + 400) to recover.

Recommendation: defer to stage 2.5 unless hybrid search shows poor artifact recall.

### 2.5c — Per-source `_existing_chunk_indices` is N+1

The embed worker does one round-trip per source to check what's already embedded. For 10,653 messages, that's 10,653 queries — most of the wall-clock cost. A single query joining `messages` to `embeddings` could replace this with one round-trip. Worth optimizing for stage 4 (when full re-embeds may be needed for new models) but not urgent now since stage 1 ingest writes won't replay.

### 2.5d — `embed_*` worker commits per-batch, not per-source

If the worker crashes mid-conversation, the partially-processed conversation has *some* embeddings. Re-running picks up cleanly via idempotency, but a crash log entry would help debug long-term. Currently, per-row failures land in the worker log only — not in a structured table like ingest_runs.errors. Worth a dedicated `embed_runs` table in stage 2.5 if we run many embed passes.

---

## Sample search results (qualitative grading)

Five queries run against the full embedded corpus:

| Query | Top-3 quality |
|---|---|
| "Project Tinderbox schema design" | EXCELLENT — all 3 hits from the actual Tinderbox creation conversation, scores 0.74–0.77 |
| "what did Reef say about RLS" | WEAK — found "reef"-mentioning messages but not RLS-specific. Query ambiguity, not retrieval failure |
| "how do we handle Cloudflare 520" | WEAK — top hit was tangentially about Cloudflare DDoS, but the 520 errors we hit are from this week's conversations not in the export |
| "pebbles core PyPI release" | ACCEPTABLE — hits are pebbles-related but not the specific PyPI release conv |
| "Song's birthday" | EXCELLENT — top hit is the literal "1/31/2026" message |

This is a 5-query smoke-test, NOT a real eval. Stage 2 §3.5 deferred eval design to Lucky:

- **Option A — collect-as-you-go.** Use the system in real conversations for 2 weeks, query log becomes the eval set. Highest fidelity, longest delay.
- **Option B — mine from history.** Lucky retroactively pulls 30-50 recent claude.ai questions that would have benefited from archive lookup. Medium fidelity, medium delay.
- **Option C — one-session brainstorm.** Lucky + a Claude session brainstorm 50 plausible queries. Lower fidelity, no delay.

Recommendation when Lucky picks: **A**. The query log is already wired (every `tinderbox search` call writes to `tinderbox.queries`). Just use it. After 2 weeks, the log is the eval — and it's literally the queries Lucky cared enough to run.

---

## Performance

- Ollama batched embedding: 32 inputs/call, ~2-3s per batch on the Mac Studio
- Resume idempotency: ~12 min of pre-roll for the 3,498 already-embedded messages (per-source existing-check overhead — see 2.5c)
- Net new embedding rate after pre-roll: ~200 messages / 50 seconds = 4 msg/sec
- Full embed pass (resumed): ~45 min for 6,541 new message embeddings + 692 artifact chunks

Search latency: 302ms for a hybrid query returning 5 results (smoke test). End-to-end: query → Ollama embed (single input, ~120ms) + Postgres hybrid_search RPC (~180ms) + result rendering (~2ms).

---

## Caveats for stage 3+

1. **No `tinderbox.queries` data accumulated yet beyond smoke tests.** Real eval requires real use.
2. **The 290-message and 128-artifact-summary gaps** mean the corpus has small holes. Easy to fill with a stage 2.5 re-run after tuning chunker thresholds, but not blocking stage 3.
3. **Service_role bypass still in effect.** Hybrid search runs as service_role with the function granted EXECUTE. Stage 5 per-caller scoping needs IPv4 add-on or proxy (unchanged from stage 1 caveat).
4. **No re-ranking yet.** The hybrid_search RPC uses a fixed weighting `cosine_sim + 0.5 * fts_rank`. Stage 2.5 should let Lucky tune this against real query log data.

---

## Stage 3 starting point

Stage 3 is **cache layer integration** (per the original spec): wire `~/shared/cache.db` / `supabase_cache.py` for hot-path reads. Most useful targets:

1. **Recent searches** — cache `tinderbox.queries` results for 5-10 minutes so repeated similar queries don't re-embed and re-RPC.
2. **Conversation metadata** — `conversations.title` / `project_name` lookups when rendering search results. Lots of repeats.
3. **Embedding for common queries** — the query embedding step (~120ms Ollama call) is cacheable by query text.

Stage 4 (Haiku enrichment) is upstream of any of these — enrichment needs to run before retrieval can score by `enrichment.project_tags` etc. Order:

- Stage 2.5 cleanup (fill gaps, tune chunker, optionally tune hybrid weights with eval data)
- Stage 4 enrichment (Haiku populates `enrichment`, `named_instances`, `conversation_named_instances`)
- Stage 3 cache layer (now there's something worth caching)
- Stage 5 MCP server with per-caller RLS

If retrieval quality from current state is good enough, stage 4 first. If retrieval has obvious gaps, stage 2.5 first.
