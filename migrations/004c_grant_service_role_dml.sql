-- Migration 004c — Grant service_role full DML on tinderbox tables.
--
-- Stage 1 expedient: the ingest worker authenticates as service_role
-- because the Mac Studio lacks IPv6 outbound and the Supabase pooler does
-- not authenticate custom Postgres roles. service_role bypasses RLS, which
-- is the documented stage-1 trade-off.
--
-- Stage 5 retrieval (per-caller MCP) needs proper roles via the IPv4 add-on
-- ($4/mo) — that lets tinderbox_owner connect directly and per-caller
-- reader roles enforce real RLS. Until then service_role is what we have.

GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES
    ON ALL TABLES IN SCHEMA tinderbox TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA tinderbox TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA tinderbox TO service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT USAGE, SELECT ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA tinderbox
    GRANT EXECUTE ON FUNCTIONS TO service_role;

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (7, '004c_grant_service_role_dml', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
