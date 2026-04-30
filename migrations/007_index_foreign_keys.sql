-- Migration 007 — Cover six unindexed foreign keys flagged by Supabase advisor lint 0001
-- Source: get_advisors performance results 2026-04-29
--
-- Each FK is a single-column reference. Indexes are non-partial b-trees.
-- All are CONCURRENTLY-safe in concept, but Supabase migration runner wraps in
-- a transaction so we use plain CREATE INDEX IF NOT EXISTS.

-- artifacts.message_id → messages(id) — used when filtering artifacts by their producing message
CREATE INDEX IF NOT EXISTS artifacts_message_id_idx
    ON tinderbox.artifacts (message_id);

-- attachments.message_id → messages(id) — used when joining attachments to their message
CREATE INDEX IF NOT EXISTS attachments_message_id_idx
    ON tinderbox.attachments (message_id);

-- conversation_named_instances.named_instance_id → named_instances(id)
CREATE INDEX IF NOT EXISTS conv_named_instances_named_instance_idx
    ON tinderbox.conversation_named_instances (named_instance_id);

-- conversations.last_ingest_run_id → ingest_runs(id)
CREATE INDEX IF NOT EXISTS conversations_last_ingest_run_idx
    ON tinderbox.conversations (last_ingest_run_id);

-- messages.privacy_class_override → privacy_classes(slug)
-- Partial index (most messages don't override) keeps the index small.
CREATE INDEX IF NOT EXISTS messages_privacy_class_override_idx
    ON tinderbox.messages (privacy_class_override)
    WHERE privacy_class_override IS NOT NULL;

-- named_instances.first_seen_conversation_id → conversations(id)
CREATE INDEX IF NOT EXISTS named_instances_first_seen_conv_idx
    ON tinderbox.named_instances (first_seen_conversation_id);

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (9, '007_index_foreign_keys', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
