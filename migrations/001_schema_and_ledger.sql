-- Migration 001 — schema namespace + pgvector + migrations ledger
-- Project: Tinderbox
-- Target: zznfzonnovkldmdrooxh (Supabase, Postgres 17.6.1)
-- Idempotent: yes (safe to re-run)

CREATE SCHEMA IF NOT EXISTS tinderbox;

-- pgvector is already installed at 0.8.0 per stage 0 prereq check.
-- This is idempotent and a no-op on the target; included for clean greenfield re-runs.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS tinderbox.schema_migrations (
    version     int         PRIMARY KEY,
    name        text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text        NOT NULL
);

COMMENT ON TABLE tinderbox.schema_migrations IS
  'Version ledger. Every DDL change ships as a numbered migration; checksum = sha256 of the migration file. Parser/worker refuses to start if migrations are pending.';

-- Self-record this migration. Checksum populated by the apply script after computing
-- the SHA-256 of this file; the placeholder below is overwritten in the same transaction.
INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (1, '001_schema_and_ledger', 'PENDING_CHECKSUM')
ON CONFLICT (version) DO NOTHING;
