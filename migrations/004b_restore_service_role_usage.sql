-- Migration 004b — Restore schema-level USAGE to service_role.
--
-- Migration 004 revoked all from PUBLIC and several Supabase-managed roles
-- (anon, authenticated) to scope tinderbox.* to tinderbox_owner only. But
-- service_role lost USAGE on the schema as a side effect, which would block
-- future migrations applied via the Supabase Management API (which auths as
-- service_role).
--
-- This restores schema USAGE only — service_role still has no per-table
-- grants until migration 004c. RLS still gates row reads.

GRANT USAGE ON SCHEMA tinderbox TO service_role;

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (5, '004b_restore_service_role_usage', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
