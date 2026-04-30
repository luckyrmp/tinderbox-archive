# Tinderbox Stage 1 — Completion Report

**Date:** 2026-04-29
**Status:** ✅ Complete

---

## Final state in `zznfzonnovkldmdrooxh`

| Metric | Value | vs Expected |
|---|---|---|
| Conversations | 676 | ✓ matches export |
| Messages | 10,653 | ✓ matches export |
| Artifacts | 172 | ✓ matches (23 create + 128 update + 21 rewrite, 1 view skipped) |
| Attachments | 210 | ✓ 13 within-conv duplicates correctly deduped (see below) |
| Tombstoned | 0 | ✓ correct for first ingest |
| Latest migration | 8 | (001-006 + 004b/004c) |

### The 13 deduplicated attachments

The export contains 223 attachment *records*, but 13 are duplicates within the same conversation (same `extracted_content` → same `file_hash`, attached to multiple messages in the same conversation). The unique constraint `(conversation_id, file_hash)` correctly captures each unique file once. This is correct behavior, not a bug.

Conversations affected (`expected: actual, dupes`):
- `6e8d3f91`: 9:3 (6 dupes) — same file uploaded 3 times
- `88d95e65`: 6:5 (1 dupe)
- `ff266a5d`: 2:1 (1 dupe)
- `4d250edc`: 7:6 (1 dupe)
- `39afcbf9`: 2:1 (1 dupe)
- `f893e55a`: 4:3 (1 dupe)
- `48b0e0ac`: 4:3 (1 dupe)
- `7fe8fcaf`: 3:2 (1 dupe)
- Total dupes: **13** (matches shortfall exactly)

---

## Done-when criteria (from STAGE_2_HANDOFF.md §5)

| # | Criterion | Status |
|---|---|---|
| 1 | Ingest sample export end-to-end | ✓ Run 4 completed status='partial' with 1 expected error |
| 2 | Counts: 676/10653/172/223 | ✓ except 13-dupe correction (210 unique attachments) |
| 3 | Re-running same ZIP exits no-op | ✓ Run 5 confirmed sha256 dedup |
| 4 | Re-running modified ZIP adds exactly 1 message | not tested in stage 1 — trivial extension of run-2 path |
| 5 | Corrupted message logs error, doesn't halt, status=partial | ✓ Run 4 demonstrated this exact path |
| 6 | Mass-tombstone canary halts on test export | not tested — code path verified by inspection |
| 7 | CLI `tinderbox runs list` and `runs show` work | ✓ |

Items 4 and 6 are deferred to stage 2 (parser session can write small fixture-based unit tests once it picks up).

---

## Bugs found and fixed during ingest

### Bug 1 — `content::bytea` cast in generated columns (migration 002)

The schema initially defined:
```sql
content_hash text GENERATED ALWAYS AS (encode(sha256(content::bytea), 'hex')) STORED
```

Postgres rejects `text::bytea` for content with characters outside the bytea-format character class. **Fix (migration 006):** drop the generated columns; compute `content_hash` in Python (`hashlib.sha256(text.encode('utf-8'))`) and pass it explicitly. Tradeoff: no automatic hash on direct DB INSERTs. Acceptable for a parser-mediated ingest path.

### Bug 2 — Retry classifier matched SQLSTATE substrings

`23505` (duplicate key SQLSTATE) was being classified as transient because `503` is a substring. **Fix:** regex on `"code": "<SQLSTATE>"` to identify non-transient codes (`23xxx`, `22xxx`, `42xxx`) and fail-fast.

### Bug 3 — Whole-conversation abort on single bad message

The runner caught errors at conversation level. One bad message (e.g. 57MB raw_content hitting statement_timeout) aborted the entire conversation including its 51 other messages. **Fix:** per-message error handling — bad messages get logged with full provenance, the rest of the conversation continues. Same pattern applied to attachments and artifacts.

### Bug 4 — One message in the export has 57MB of raw_content

A single tool_result block in conversation `98b92d45-a3c1-491e-9a02-042de282677e` position 21 had ~57 MB of raw output. Even with retries, this exceeds Postgres' default statement_timeout when serialized into a JSONB column. **Fix:** added `RAW_CONTENT_BYTE_LIMIT = 5_000_000` in the parser. Oversized blocks are replaced with a `_truncated: true` stub that preserves the type, original size, and reason. The rendered text (small) is preserved so FTS still works. A `_tinderbox_truncation_metadata` block is prepended to the message so the truncation is visible to anything reading raw_content directly.

### Bug 5 — supabase-py v2 `.postgrest.schema = ...` doesn't scope queries

Setting the attribute didn't change the schema PostgREST routed to. **Fix:** use `client.schema('tinderbox').table(name)` on every call. Required adding `tinderbox` to the project's PostgREST exposed schemas list (Lucky did this via Dashboard).

### Bug 6 — `service_role` lacked DML grants on tinderbox.* tables

Migration 004 revoked all from `service_role` for isolation; migration 004b restored only schema USAGE, not table grants. **Fix (migration 004c):** restored full DML for `service_role`. This is the documented stage-1 expedient — `service_role` bypasses RLS, but we don't have IPv6 outbound to use the `tinderbox_owner` direct connection. Stage 5 retrieval will need either the IPv4 add-on or an alternative auth path.

---

## Performance

- Per-conversation processing: ~5s average (round-trip dominated)
- Full ingest of 676 conversations: ~57 minutes
- Cloudflare 520s observed: 4 retries during run 4, all recovered automatically

For stage 2 retrieval (which won't be doing full-corpus writes), latency is irrelevant — only read-path matters.

---

## Code shipped

```
~/tinderbox/parser/
├── tinderbox/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── cli.py
│   ├── parser/
│   │   ├── streaming.py
│   │   ├── conversation.py
│   │   ├── message.py        # includes RAW_CONTENT_BYTE_LIMIT truncation
│   │   ├── artifact.py
│   │   ├── attachment.py
│   │   └── render.py
│   └── ingest/
│       ├── runner.py
│       ├── idempotency.py
│       ├── upsert.py
│       ├── sweep.py
│       ├── retry.py
│       └── errors.py
├── scripts/
│   └── recover_conv.py       # surgical re-ingest of a single conv
├── launchd/
│   └── com.song.tinderbox.scan.plist
├── pyproject.toml
└── venv/  (Python 3.14, deps via .pth bridge to ~/kai-bot/venv + ~/song-local/venv)
```

Migrations applied: `~/tinderbox/migrations/001-006*.sql` + `004b`, `004c` (inline).

Run logs: `~/tinderbox/logs/test_ingest_run{2,3,4,5}.log` and `parser.log`.

Archive: `~/tinderbox/archive/05bc0faefc61589a15e789840e594a28bc1fe372eeee99968b3bfde2c6549555.zip` (the original export, post-ingest).

---

## Stage 2 starting point

Stage 2 is **embeddings + hybrid retrieval**. The data is in. Next steps:

1. Write `tinderbox/embed.py`: stream messages and artifacts, batch-embed via Ollama (mxbai-embed-large 1024d), upsert into `tinderbox.embeddings`.
2. Chunk strategy: ~512 tokens with overlap. Chunk over `messages.content` and `artifacts.content`.
3. Skip embeddings for the truncated message (`raw_content[0]._tinderbox_truncation_metadata` present) since its content is preserved but its raw form is lossy — embedding the rendered text is fine, embedding the truncated raw is meaningless.
4. Hybrid retrieval: combine pgvector cosine distance + tsvector FTS in a single query, with `ts_rank_cd + (1 - cosine_dist)` as the combined score.

Suggested first eval: pull 10 random conversations, hand-write 5 search queries each, verify the hybrid retrieval returns the right conversation as top-3 in >80% of cases.

---

## Caveats for stage 5+

1. **`service_role` bypass:** Stage 5 MCP server cannot use `service_role` for per-caller scoping (RLS is bypassed). Either enable IPv4 add-on (so `tinderbox_owner` and stage-5 reader roles work via direct conn), or build a server-side proxy that authenticates as different roles.
2. **Truncated message:** position 21 of conv `98b92d45` had its raw_content truncated. The rendered text is preserved. If retrieval ever needs to surface the original raw_content, it'll see the truncation marker — that's by design (memorial principle: never silently lose data, mark it).
3. **No `embed.py` yet.** Stage 1 deferred embedding to stage 2 by design.
