-- Migration 004 — tinderbox_owner role, grants, RLS enable
--
-- The role password is generated at apply time and substituted via psql variable
-- :tinderbox_owner_password (psql --variable=tinderbox_owner_password=...).
-- The Supabase SQL runner does not support psql variables, so when applied via
-- the MCP apply_migration tool, the password is interpolated into a copy of this
-- file before it's submitted. Either way, the password never lands in source control.
--
-- Idempotent: yes, but the role is created with NOLOGIN initially if it already exists
-- (avoiding accidental password resets on re-run).

-- ============================================================================
-- tinderbox_owner role
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tinderbox_owner') THEN
        EXECUTE format('CREATE ROLE tinderbox_owner LOGIN PASSWORD %L', :'tinderbox_owner_password');
    END IF;
END $$;

-- Reset/ensure baseline grants every run (idempotent).
GRANT USAGE ON SCHEMA tinderbox TO tinderbox_owner;
GRANT ALL ON ALL TABLES IN SCHEMA tinderbox TO tinderbox_owner;
GRANT ALL ON ALL SEQUENCES IN SCHEMA tinderbox TO tinderbox_owner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA tinderbox TO tinderbox_owner;

-- Default privileges so future tables/sequences in this schema auto-grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT ALL ON TABLES TO tinderbox_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT ALL ON SEQUENCES TO tinderbox_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT EXECUTE ON FUNCTIONS TO tinderbox_owner;

-- Explicitly REVOKE from PUBLIC and the Supabase default app roles.
-- agent stack uses anon / authenticated / service_role for its existing tables;
-- none of those should reach tinderbox.*.
REVOKE ALL ON SCHEMA tinderbox FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA tinderbox FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA tinderbox FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA tinderbox FROM anon';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA tinderbox FROM anon';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA tinderbox FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA tinderbox FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA tinderbox FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA tinderbox FROM authenticated';
    END IF;
END $$;

-- service_role intentionally NOT revoked — it's the Supabase superuser key and is
-- used by the MCP tool to apply migrations. Revoking it would lock us out.
-- Per-caller scoping (Reef vs Lucky vs Song) lands in stage 5 with dedicated roles.

-- ============================================================================
-- RLS — enable on every table; tinderbox_owner has unrestricted access
-- Per-caller policies (filter by privacy_class) land in stage 5.
-- ============================================================================
ALTER TABLE tinderbox.schema_migrations             ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.privacy_classes               ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.ingest_runs                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.conversations                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.messages                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.artifacts                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.attachments                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.embeddings                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.enrichment                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.named_instances               ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.conversation_named_instances  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tinderbox.queries                       ENABLE ROW LEVEL SECURITY;

-- Drop-and-recreate so re-running migration 004 is safe and policies stay in sync.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT unnest(ARRAY[
            'schema_migrations', 'privacy_classes', 'ingest_runs',
            'conversations', 'messages', 'artifacts', 'attachments',
            'embeddings', 'enrichment', 'named_instances',
            'conversation_named_instances', 'queries'
        ])
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tinderbox_owner_all ON tinderbox.%I', t);
        EXECUTE format(
            'CREATE POLICY tinderbox_owner_all ON tinderbox.%I FOR ALL TO tinderbox_owner USING (true) WITH CHECK (true)',
            t
        );
    END LOOP;
END $$;

-- ============================================================================
-- record this migration
-- ============================================================================
INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (4, '004_role_grants_rls', 'PENDING_CHECKSUM')
ON CONFLICT (version) DO NOTHING;
