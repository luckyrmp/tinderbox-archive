-- Migration 006 — Replace generated content_hash columns with plain text.
--
-- Migration 002 originally defined `content_hash` on `messages` and `artifacts`
-- as:
--     text GENERATED ALWAYS AS (encode(sha256(content::bytea), 'hex')) STORED
--
-- The `content::bytea` cast fails on text containing characters outside the
-- bytea-format character class — i.e. plenty of real claude.ai content.
-- `convert_to(content, 'UTF8')` is not strictly IMMUTABLE per Postgres'
-- generated-column rules, so we can't fix the cast in-place.
--
-- Resolution: drop the generated columns; the parser computes the hash in
-- Python (hashlib.sha256(content.encode("utf-8")).hexdigest()) and passes it
-- explicitly on insert.
--
-- See `parser/tinderbox/ingest/upsert.py:_content_hash`.

-- Truncate any partial data — at the time this migration was applied
-- (during Lucky's stage 1 ingest), there was test data in the tables that
-- needed to be cleared. Fresh installations have nothing to truncate; the
-- TRUNCATE is a no-op.
-- For a fresh install, this whole TRUNCATE block is a no-op since the tables
-- are empty. For an existing install with bad rows, it clears them so the
-- new content_hash column starts clean.
TRUNCATE TABLE
    tinderbox.attachments,
    tinderbox.artifacts,
    tinderbox.messages,
    tinderbox.embeddings,
    tinderbox.enrichment,
    tinderbox.conversation_named_instances,
    tinderbox.conversations,
    tinderbox.ingest_runs
CASCADE;

-- messages.content_hash: drop generated, add plain
ALTER TABLE tinderbox.messages DROP COLUMN content_hash;
ALTER TABLE tinderbox.messages ADD COLUMN content_hash text;

-- artifacts.content_hash: same
ALTER TABLE tinderbox.artifacts DROP COLUMN content_hash;
ALTER TABLE tinderbox.artifacts ADD COLUMN content_hash text;

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (8, '006_drop_content_hash_generated', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
