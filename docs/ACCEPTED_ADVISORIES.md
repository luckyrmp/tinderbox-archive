# Tinderbox — Accepted Supabase Advisories

This file documents Supabase advisor findings that are intentionally **not acted on** for Tinderbox, with the reasoning. Re-evaluate dates marked.

Migration 007 already covered the 6 unindexed-foreign-key findings (lint 0001). What remains is below.

---

## Unused indexes (lint 0005) — 20 findings

**Status:** ACCEPTED, re-evaluate after stage 2 has been live ≥ 2 weeks.

**Date accepted:** 2026-04-29
**Re-evaluate by:** 2026-05-20 (after stage 2 retrieval has been in real use)

### Why these aren't dropped now

The `unused_index` lint fires on `pg_stat_user_indexes.idx_scan = 0` — i.e. the index has never been hit. The DB has been live for less than 24 hours and stage 1 only does writes. The query workload that exercises these indexes hasn't started yet.

| Index | Why it exists |
|---|---|
| `embeddings_embedding_hnsw` | Cornerstone of stage 2 hybrid retrieval. Cosine similarity on `mxbai-embed-large` vectors. **Do not drop.** |
| `embeddings_source_idx` | Used by stage 5 to backtrack from a hit chunk to its source message/artifact. |
| `messages_tsv_gin`, `artifacts_tsv_gin`, `attachments_tsv_gin` | Full-text search half of hybrid retrieval. Used by stage 2 query path. |
| `messages_raw_content_gin` | Queries that filter on JSONB block types (e.g. "find all conversations that contain a thinking block"). Stage 4 enrichment. |
| `conversations_updated_at_idx`, `conversations_project_*`, `conversations_superseded_by_idx`, `conversations_raw_metadata_gin` | Browsing / filtering paths for the stage 5 MCP server (recency, project scope, supersession traversal, ad-hoc raw_metadata queries). |
| `artifacts_conversation_idx` | Fetch all artifacts for a conversation — common stage 5 read path. |
| `attachments_file_hash_idx` | Cross-conversation dedup lookup ("does any conversation already have this hash?"). Used by stage 4 deduplication. |
| `enrichment_*` | Stage 4 enrichment hasn't run yet. Indexes will be exercised when project_tags / client_tags filtering goes live. |
| `ingest_runs_status_started_idx` | Used by `tinderbox runs list --status partial` and the orphan sweep. Will fire on every CLI call once we start running them in anger. |
| `queries_*` | Stage 5 only. Not exercised yet because no MCP server has been deployed. |

### Action

Re-run `get_advisors` after stage 2 retrieval has been in real use for ≥ 2 weeks. Anything that still shows zero scans by 2026-05-20 is a real candidate for removal. Until then, dropping any of these would be premature optimization (and dropping `embeddings_embedding_hnsw` specifically would break stage 2).

---

## Multiple permissive policies on `public.*` for `tinderbox_owner` (lint 0006) — 7 findings

**Status:** OUT OF SCOPE for Tinderbox.

**Date noted:** 2026-04-29

The advisor flags 7 `public.*` tables (`agent_identity`, `boundaries`, `context_windows`, `people`, `person_context`, `robert_identity`, `routing_signals`) where two overlapping permissive SELECT policies exist for several roles, including `tinderbox_owner`. The underlying issue is the agent stack's `_read` / `_write` policy split, not anything Tinderbox introduced — `tinderbox_owner` only appears because the lint enumerates every role.

This is the agent stack's responsibility. If/when the agent stack consolidates those policies, the lint will clear for `tinderbox_owner` automatically. Tinderbox doesn't grant `tinderbox_owner` membership in any public-schema role, so the role inherited those policies passively at creation.

No action required from Tinderbox. Out of scope for stage 1.5.

---

## How to refresh this triage

```
mcp__claude_ai_Supabase__get_advisors(project_id=zznfzonnovkldmdrooxh, type=security)
mcp__claude_ai_Supabase__get_advisors(project_id=zznfzonnovkldmdrooxh, type=performance)
```

Filter to entries where `metadata.schema = "tinderbox"`. For each new finding, classify as:

- **APPLY** — ship a migration to fix it
- **ACCEPT** — document here with an explicit reason and a re-evaluate date
- **DEFER** — anything tied to the IPv4/RLS resolution coming in stage 5

Do not blanket-apply advisor recommendations. Each one needs a one-line judgment.
