# Tinderbox Stage 2 Handoff — Parser & Ingest Worker

**From:** Reef (stage 1 session, 2026-04-28)
**For:** Claude session writing the parser
**Mode:** This is your briefing. The schema is live. Read everything below before writing code.

---

## What stage 1 delivered

The `tinderbox` schema is live on Supabase project `zznfzonnovkldmdrooxh` (Postgres 17.6.1, pgvector 0.8.0, hnsw available). Five migrations applied:

| Migration | What it created |
|---|---|
| 001 | `tinderbox` schema, pgvector extension, `schema_migrations` ledger |
| 002 | `privacy_classes` (seeded), `ingest_runs`, `conversations`, `messages`, `artifacts`, `attachments` |
| 003 | `embeddings`, `enrichment`, `named_instances`, `conversation_named_instances`, `queries` |
| 004 | `tinderbox_owner` role (LOGIN), grants, RLS enabled on all 12 tables, policies scoped to `tinderbox_owner` only |
| 004b | Restored `service_role` USAGE on schema (so MCP tool can apply future migrations) |

### Key files on Mac Studio

| Path | What |
|---|---|
| `~/tinderbox/discovery/stage_0_report.md` | Environment discovery report |
| `~/tinderbox/migrations/001-004*.sql` | Applied migration files with checksums |
| `~/projects/pending/Project Tinderbox/stage_1_schema_proposal.md` | Full schema doc with design principles, table specs, resolved questions |
| `~/.secrets/shared.env` | Contains `TINDERBOX_*` vars (DB URL, embedding model, blob root, Ollama endpoint) |

### Connection details

```
# These are in ~/.secrets/shared.env — load them, don't hardcode.
TINDERBOX_DB_URL=postgresql://tinderbox_owner:<password>@db.zznfzonnovkldmdrooxh.supabase.co:5432/postgres?sslmode=require
TINDERBOX_BLOB_ROOT=/Users/song/tinderbox/blobs
TINDERBOX_OLLAMA_URL=http://localhost:11434
TINDERBOX_EMBEDDING_MODEL=mxbai-embed-large
TINDERBOX_EMBEDDING_DIM=1024
```

**CRITICAL — IPv6 connectivity issue:** The Mac Studio lacks IPv6 outbound. `TINDERBOX_DB_URL` (direct connection via `db.*.supabase.co`) will fail with "No route to host" because Supabase direct connections resolve to IPv6 only. The Supabase Supavisor pooler (IPv4) does NOT authenticate custom Postgres roles — only Supabase-managed ones.

**Workaround for stage 2 ingest worker:** Use `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` (already in `shared.env`) via the Supabase Python client (`supabase-py`) or the REST API. The `service_role` key bypasses RLS, which is acceptable for the ingest worker since it writes all privacy classes. The `TINDERBOX_DB_URL` with `tinderbox_owner` is reserved for stage 5 per-caller retrieval (which will need the IPv4 add-on or a different auth path).

**Alternative:** Use `psycopg2` with the pooler connection string as `postgres` user: `postgresql://postgres.zznfzonnovkldmdrooxh:<project_db_password>@aws-0-us-east-2.pooler.supabase.com:5432/postgres`. The project DB password is available in Supabase Dashboard → Project Settings → Database. This also bypasses RLS (postgres superuser).

**Recommended approach:** Use the Supabase Python client for DML (INSERT/UPSERT) since it handles auth, retries, and connection pooling. Set `schema='tinderbox'` on the client. Example:

```python
from supabase import create_client
import os

url = os.environ['SUPABASE_URL']
key = os.environ['SUPABASE_SERVICE_KEY']
sb = create_client(url, key, options={'schema': 'tinderbox'})
```

---

## Design principles the parser MUST honor

Read the full list in `~/projects/pending/Project Tinderbox/stage_1_schema_proposal.md` under "Design principles." Summary:

1. **Provenance is the answer; content is evidence.** Every record carries full source attribution (conversation_id, message position, timestamp, export_id).
2. **Content is immutable.** Messages, artifacts, attachments are INSERT-only. Never UPDATE content after ingest.
3. **Enrichment is opinion.** The parser does NOT write to `enrichment`, `named_instances`, or `conversation_named_instances` — that's stage 4. Parser only writes to: `ingest_runs`, `conversations`, `messages`, `artifacts`, `attachments`.
4. **Privacy defaults to `personal`.** Every conversation gets `privacy_class = 'personal'` unless the parser has a project→privacy mapping. Do not default to `public`.
5. **Read-mostly.** Tinderbox is hydrated from exports, not user input.

---

## What the parser needs to do

### Input

A claude.ai data export (ZIP or extracted directory). Lucky triggers an export from claude.ai Settings → Account → Export Data. The export arrives as a ZIP containing JSON files — one per conversation.

**Before writing the parser:** Lucky needs to provide a sample export so you can inspect the actual JSON structure. The schema was designed from prior knowledge of the format, but field names, nesting, and edge cases need to be confirmed against a real export. **Do not guess the format. Ask Lucky for a sample.**

### Output

For each ingest run:

1. **Create an `ingest_runs` row** at the start (`status='running'`). Hash the ZIP → `export_sha256`. If the hash already exists, exit cleanly (idempotent).

2. **For each conversation in the export:**
   - UPSERT into `conversations` (unique on `export_id`). Set `last_seen_in_export_at = now()`.
   - `privacy_class` defaults to `'personal'`. If a project→privacy mapping config exists, apply it.
   - Parse `project_id` / `project_name` from the export envelope if present.
   - Store everything else in `raw_metadata` JSONB.

3. **For each message in each conversation:**
   - INSERT into `messages` (skip if `(conversation_id, export_id)` already exists).
   - `content` = rendered text (human-readable). `raw_content` = original JSON block array (preserves tool_use, thinking, citations).
   - `position` = 0-indexed ordering within the conversation.
   - `role` must be one of: `human`, `assistant`, `tool_use`, `tool_result`, `system`.

4. **For each artifact in each conversation:**
   - INSERT into `artifacts` (skip if `(artifact_id, version)` already exists).
   - Parse artifact commands (`create`, `update`, `rewrite`) into separate versioned rows.
   - Link `message_id` to the message that produced this artifact version.

5. **For each attachment:**
   - Copy the binary to `$TINDERBOX_BLOB_ROOT/<conversation_export_id>/<file_hash>`.
   - INSERT into `attachments` (skip if `(conversation_id, file_hash)` already exists).
   - `blob_path` is RELATIVE: `<conversation_export_id>/<file_hash>`.
   - If the file is text-extractable (PDF, docx, txt, csv), populate `extracted_text`.

6. **Tombstone sweep (end of run):**
   ```sql
   UPDATE tinderbox.conversations
   SET deleted_upstream = true, deleted_upstream_at = now()
   WHERE deleted_upstream = false
     AND last_seen_in_export_at < (SELECT started_at FROM tinderbox.ingest_runs WHERE id = :run_id);
   ```
   **Mass-tombstone canary:** if >5% of conversations flip to `deleted_upstream = true` in one run, log a WARNING — almost certainly a parser regression, not real deletion.

7. **Finalize the `ingest_runs` row:** set `completed_at`, `status`, counters (`conversations_new`, `messages_new`, etc.), `errors` array.

### Embeddings (stage 2 stretch goal, not blocking)

After ingest, optionally embed messages and artifact chunks:
- Model: `mxbai-embed-large` via Ollama at `http://localhost:11434/api/embeddings`
- Dim: 1024 (must match `vector(1024)` in the schema)
- Chunk long content (>512 tokens) with overlap
- UPSERT into `embeddings` (unique on `(source_type, source_id, chunk_index, embedding_model)`)
- `source_type` = `'message'` or `'artifact'`

Embedding can run as a separate pass after ingest completes. It's safe to re-run (idempotent).

---

## What the parser must NOT do

- Write to `enrichment`, `named_instances`, `conversation_named_instances`, or `queries` — those are stage 4/5.
- UPDATE message `content` or artifact `content` after initial insert.
- Set `privacy_class = 'public'` as a default.
- Delete any row, ever.
- Hardcode paths or credentials — read from `~/.secrets/shared.env`.
- Guess the export format — work from a real sample.

---

## Suggested project structure

```
~/tinderbox/
├── discovery/           # stage 0 report (exists)
├── migrations/          # DDL files (exists, 001-004)
├── blobs/               # attachment binaries (created by parser)
├── config.py            # loads shared.env, exposes TINDERBOX_* vars
├── parser.py            # export ZIP → parsed conversations/messages/artifacts
├── ingest.py            # orchestrator: opens run, calls parser, writes to DB, sweeps tombstones
├── embed.py             # optional: post-ingest embedding pass
└── requirements.txt     # supabase-py, python-dotenv, hashlib (stdlib), etc.
```

---

## Open items for stage 2 session

1. **Get a sample export from Lucky.** Inspect the JSON structure before writing parser logic. Do not assume field names.
2. **Decide on supabase-py vs psycopg2+pooler for DB access.** Recommendation: supabase-py with `schema='tinderbox'` and `service_role` key.
3. **Artifact parsing is the tricky part.** Claude artifacts are embedded in the assistant message's content blocks as tool_use calls with specific types. The parser needs to extract these and reconstruct the versioning chain. A sample export will reveal the exact structure.
4. **Attachment text extraction.** Decide which formats to support in v1. Recommendation: plain text, PDF (via `pymupdf` or `pdfplumber`), CSV. Skip docx/images for now.
