-- Migration 005 — Pre-parser fixes from stage 2 handoff
-- Two corrections based on real export data (676 convos, 10,653 messages):
--
-- 1. attachments.blob_path → nullable.
--    The claude.ai export ships extracted_content (text) directly, not binary files.
--    blob_path is only relevant for future formats that include actual binaries.
--
-- 2. messages.role constraint → relaxed to ('human', 'assistant') only.
--    The export only uses 'human' and 'assistant' as sender values.
--    tool_use, tool_result, thinking are content block types inside raw_content JSONB,
--    not separate message roles. Tighter constraint catches parser bugs.
--
-- Source: ~/tinderbox/STAGE_2_HANDOFF.md §1.4, §2

BEGIN;

-- 005a: relax blob_path NOT NULL
ALTER TABLE tinderbox.attachments ALTER COLUMN blob_path DROP NOT NULL;

-- 005b: tighten role constraint to match reality
ALTER TABLE tinderbox.messages DROP CONSTRAINT IF EXISTS messages_role_check;
ALTER TABLE tinderbox.messages
  ADD CONSTRAINT messages_role_check
  CHECK (role IN ('human', 'assistant'));

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (6, '005_pre_parser_fixes', 'PENDING_CHECKSUM')
ON CONFLICT (version) DO NOTHING;

COMMIT;
