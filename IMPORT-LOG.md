# Tinderbox Import Log

One entry per ingest of a claude.ai export. See `tinderbox.ingest_runs` (DB) for the
machine-readable audit trail; this file is the human-readable summary.

---

## 2026-08-08 — All-time re-export (90-day export superseded)

**Source:** All-time claude.ai export, downloaded to
`~/Downloads/data-9ba1ee69-23db-4446-a18c-4d6647679f35-1786226408-a3656f34-batch-0000/`
(811 conversations, 2024-10-25 → 2026-08-08). Archived post-ingest at
`~/tinderbox/archive/98593eb6a0ee523a934d2a3357953470368c6cdfa9c7090c5fcf48e21eae5dbf.zip`.

A 90-day export was pulled first but rejected before ingest: the archive was ~70 days
stale, and a partial-window export would have tripped (and, if forced past, satisfied)
the mass-tombstone canary — `apply_tombstones` marks anything absent from the export as
`deleted_upstream`, which assumes every export is a full one. Confirmed via a prior
failed run (`057efda9`, 2026-06-02) that hit exactly this: "would tombstone 676 of 735."
Re-pulled as an all-time export instead of patching the pipeline to skip the sweep.

**Ingest run:** `12816250-bf80-4f2a-8c3f-62797b3bc6cd` (status: `partial`, 1 recorded
error — see below, resolved).

| Metric | Before | After | Delta |
|---|---|---|---|
| Conversations (total / active) | 735 / 735 | 811 / 811 | +76 new, 0 tombstoned |
| Conversations updated (metadata refresh) | — | 735 | — |
| Conversations with appended messages | — | 4 | +206 messages |
| Full-duplicate conversations (no-op) | — | 731 | — |
| Messages | 11,383 | 12,420 | +1,037 |
| Artifacts | 172 | 172 | +0 |
| Attachments | 225 | 235 | +10 |
| Message embeddings (chunk rows) | 10,823 | 12,032 | +1,209 |
| Artifact embeddings (chunk rows) | 692 | 692 | +0 |

Export/DB message-count cross-check: export total messages = 12,420; DB messages
post-ingest = 12,420. Exact match, confirming lossless ingest.

**The 1 recorded ingest error:** message `019c2104` in conversation `124e553a`
("Penetration testing script for web application") got a Cloudflare 403/WAF block
(`JSON could not be generated`, HTML response body) on every retry attempt — almost
certainly the message content matching WAF signature rules (it discusses SQLi/XSS/path
traversal test categories). Direct verification via `psycopg2` against
`TINDERBOX_DB_URL` (bypassing the Cloudflare-fronted PostgREST path) showed the row was
already present with correct content — the origin had committed the write despite the
client seeing a blocked response. No data loss; the ingest run's `partial` status is a
false negative for this one row.

**Bugs found and fixed (committed `95238d6`, pushed to `origin/main`):**

1. `tinderbox/ingest/retry.py` — `is_transient()` didn't classify Postgres
   statement-timeout (`57014`) as retryable, so any embeddings-upsert batch that hit one
   failed immediately instead of retrying with backoff. Silently dropped ~200 embedding
   rows during the first `tinderbox embed` pass under HNSW-index write load before the
   fix. Re-running `tinderbox embed` after the fix (idempotent, per-chunk) recovered all
   of them.
2. `tinderbox/embed/worker.py` — the char-based chunking heuristic
   (`SOFT_INPUT_LIMIT_CHARS = 1400`, ~3 chars/token) underestimates token density for
   dense non-prose content (JSON, terminal output, code), so 16 messages exceeded
   Ollama's context window even after truncation and were silently marked
   `chunks_failed` with no retry. Added a truncate-and-retry fallback (halve the text,
   up to ~6 attempts) with explicit `WARNING`-level logging on recovery and `ERROR` if
   truly unrecoverable — no silent skips. All 16 recovered on the first halving.

**Verification:**
- Zero real embedding gaps: audited every message/artifact directly (not via counters).
  12,032/12,032 non-empty messages embedded; 44/44 non-empty artifacts embedded. The
  remaining 388 messages and 128 artifacts have zero embeddings because they have zero
  content (pre-existing, mostly Sept–Oct 2025 artifacts/messages predating this import —
  not a regression).
- Most recent conversation in DB: 2026-08-08 ("Graph engineering fundamentals" and "TV
  recycling drop-off near 90250"), i.e. today.
- Spot-check hybrid searches (`tinderbox search`), all relevant:
  - "Forge MCP" → top hits from "Claude plugins evaluation," "Flashforge MCP."
  - "CubeBot" → top 5 hits all from "LuckAiBot," directly on-topic.
  - "song-autonomous workspace" → rank #2 is today's "Graph engineering fundamentals"
    conversation, confirming freshest-data reaches embedding/retrieval end-to-end.

## 2026-10-26 — full all-time Claude export (run 0e4b1e07)

- Export: `exports/conversations-000-2026-10-27.zip` (sha256 a9d54826…), all-time export by Reef.
- First attempt (run f8381724) failed at startup and wrote nothing; retry 0e4b1e07 completed.
- Ingest: 109 new/updated conversations of 920 seen, 2,359 messages, 10 attachments, 0 errors. Canary did not trip.
- Embed: 2,264 new message chunks; a few transient Supabase statement timeouts recovered on retry. `ALL DONE exit=0` at 21:43 PT.
- Archive now: 920 conversations (all active), 14,779 messages, 14,988 embeddings; newest conversation Oct 26, 2026 (7:29 PM PT).
- Minor: harmless logging TypeError in `ingest/runner.py:175` ("Invalidated %d cached search entries", n is None). Cosmetic, not fixed.
- Next: weekly export + import routine.

## 2026-11-01 — weekly all-time Claude export (FAILED, not ingested)

- Export: `exports/conversations-000-20261101.zip` (sha256 373f177f…), conversations.json at root (~200 MB, 920+ conversations), requested 6:10 PM PT.
- Run f567d2b1: launched via nohup from an agent shell; process was killed when the shell session ended (no exit line). Left `running`; marked `failed` by hand so the sha could be released.
- Runs 9b5f40df and 8fbbf18a (via one-shot LaunchAgent): after ~2 min of parsing, every Supabase call failed with `httpx.ConnectError [Errno 8] nodename nor servname provided` for the whole run (920 conversation errors, exit=1). The Supabase host resolves fine from the same Mac immediately before/after. Both left `running`, marked `failed`. Embed not run.
- DB unchanged: 920 conversations, 14,779 messages.
- Next: investigate the in-process DNS failure (likely after the long parse — connection pool/resolver under load), then re-run ingest. Not overridden.

## 2026-11-01 (retry) — weekly all-time Claude export (run d6d54b3c)

- Export: sha256 373f177f…, 927 conversations / 15,568 messages; ZIP archived to `archive/373f177f….zip`.
- **Root cause of the Errno 8 failures:** `db.table()` called supabase-py `.schema()` on every request, which builds a new PostgREST client + httpx session each time and never closes it. Sockets leaked until the process hit launchd's 256-fd soft limit; macOS getaddrinfo then fails with `[Errno 8] nodename nor servname provided`. (Terminal shells have a huge fd limit, so it never showed there.) Fix `e770466`: cache one schema client. Verified fds flat at ~63 for the whole run.
- Side effect of connection reuse: Supabase sends HTTP/2 GOAWAY every ~20k streams (`ConnectionTerminated`), which retry didn't classify as transient → 3 messages skipped. Fix `62d9c7a`: treat ConnectionTerminated/RemoteProtocolError as transient. `scripts/recover_conv.py` on the 3 conversations found nothing missing; DB message count equals export (15,568).
- Ingest: status `partial` (only those 3 errors), 7 new + 920 updated conversations, 789 new messages, 1 attachment. Embed: 748 new message chunks, `EMBED exit=0`. Ran via one-shot LaunchAgent (removed after).
- **Incident (mine):** running `pytest tests` executed the live integration test `test_modified_zip.py`, which calls `ingest_run(canary_override=True)` against prod; its sweep tombstoned all 920 existing conversations (deleted_upstream_at 2026-11-02T04:12:57Z). No rows deleted. Reverted exactly that batch (all 920 were present in this export). Live tests now opt-in via `tests/conftest.py` (`TINDERBOX_LIVE_TESTS=1`).
- Archive now: 927 conversations (all active), 15,568 messages; newest conversation "3D print plan search", updated Nov 1, 2026 6:12 PM PT.
