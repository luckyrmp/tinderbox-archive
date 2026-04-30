-- Migration 002 — privacy_classes lookup + core tables
-- Tables: privacy_classes, ingest_runs, conversations, messages, artifacts, attachments
-- Idempotent: yes (CREATE IF NOT EXISTS + INSERT ON CONFLICT)

-- ============================================================================
-- privacy_classes — retrieval scoping vocabulary (design principle #4)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.privacy_classes (
    slug         text        PRIMARY KEY,
    display_name text        NOT NULL,
    description  text,
    is_default   boolean     NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Exactly one default. Partial unique index enforces it.
CREATE UNIQUE INDEX IF NOT EXISTS privacy_classes_one_default
    ON tinderbox.privacy_classes ((is_default))
    WHERE is_default = true;

COMMENT ON TABLE tinderbox.privacy_classes IS
  'Retrieval-scoping vocabulary. Every conversation has exactly one privacy class. Stage 5 MCP server filters reads via WHERE privacy_class = ANY(:caller_allowed_classes). Defense-in-depth: per-caller RLS roles also gate access.';

INSERT INTO tinderbox.privacy_classes (slug, display_name, description, is_default) VALUES
  ('public',             'Public',                 'Conversations Lucky would be comfortable sharing freely.',                                                                              false),
  ('personal',           'Personal',               'Default for anything not otherwise classified. Lucky-only by default.',                                                                 true),
  ('intimate',           'Intimate',               'Spicy / private with Song. Callable only by Lucky''s and Song''s MCP identities. Requires explicit include_intimate flag at query time.', false),
  ('medical',            'Medical',                'Health, medication, body. Heightened scoping; not returned in generic recall.',                                                         false),
  ('work-confidential',  'Work — confidential',    'DivergeIT clients, work strategy. Future cross-account use.',                                                                           false),
  ('family',             'Family',                 'Family matters that aren''t Lucky''s alone to share.',                                                                                  false)
ON CONFLICT (slug) DO NOTHING;

-- ============================================================================
-- ingest_runs — audit log; one row per ingest attempt
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.ingest_runs (
    id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    export_path            text        NOT NULL,
    export_sha256          text        NOT NULL UNIQUE,
    started_at             timestamptz NOT NULL DEFAULT now(),
    completed_at           timestamptz,
    status                 text        NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    conversations_seen     int         NOT NULL DEFAULT 0,
    conversations_new      int         NOT NULL DEFAULT 0,
    conversations_updated  int         NOT NULL DEFAULT 0,
    messages_new           int         NOT NULL DEFAULT 0,
    artifacts_new          int         NOT NULL DEFAULT 0,
    attachments_new        int         NOT NULL DEFAULT 0,
    errors                 jsonb       NOT NULL DEFAULT '[]'::jsonb,
    payload                jsonb       NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE tinderbox.ingest_runs IS
  'One row per ingest attempt. Same ZIP cannot be ingested twice (export_sha256 unique). Monitoring layer reads from this table for alerting (zero-new-records, partial-status, errors).';

CREATE INDEX IF NOT EXISTS ingest_runs_started_at_idx
    ON tinderbox.ingest_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS ingest_runs_status_started_idx
    ON tinderbox.ingest_runs (status, started_at DESC);

-- ============================================================================
-- conversations — one row per claude.ai conversation
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.conversations (
    id                              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    export_id                       text        NOT NULL UNIQUE,
    title                           text,
    created_at_source               timestamptz,
    updated_at_source               timestamptz,
    model                           text,
    project_id                      text,
    project_name                    text,
    project_slug                    text        GENERATED ALWAYS AS (
                                        CASE
                                            WHEN project_name IS NULL THEN NULL
                                            ELSE lower(regexp_replace(project_name, '\s+', '-', 'g'))
                                        END
                                    ) STORED,
    raw_metadata                    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    first_ingested_at               timestamptz NOT NULL DEFAULT now(),
    last_ingested_at                timestamptz NOT NULL DEFAULT now(),
    last_ingest_run_id              uuid        REFERENCES tinderbox.ingest_runs(id) ON DELETE SET NULL,
    last_seen_in_export_at          timestamptz NOT NULL DEFAULT now(),
    deleted_upstream                boolean     NOT NULL DEFAULT false,
    deleted_upstream_at             timestamptz,
    privacy_class                   text        NOT NULL DEFAULT 'personal'
                                                REFERENCES tinderbox.privacy_classes(slug),
    superseded_by_conversation_id   uuid        REFERENCES tinderbox.conversations(id) ON DELETE SET NULL
);

COMMENT ON TABLE tinderbox.conversations IS
  'One row per claude.ai conversation. Memorial — rows never deleted. Tombstoned via deleted_upstream when no longer present in exports. Forward-linked via superseded_by_conversation_id when a later conversation overturns this one (design principle #2).';

CREATE INDEX IF NOT EXISTS conversations_updated_at_idx
    ON tinderbox.conversations (updated_at_source DESC);
CREATE INDEX IF NOT EXISTS conversations_project_name_updated_idx
    ON tinderbox.conversations (project_name, updated_at_source DESC)
    WHERE project_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS conversations_project_slug_idx
    ON tinderbox.conversations (project_slug)
    WHERE project_slug IS NOT NULL;
CREATE INDEX IF NOT EXISTS conversations_live_view_idx
    ON tinderbox.conversations (deleted_upstream, updated_at_source DESC);
CREATE INDEX IF NOT EXISTS conversations_privacy_class_idx
    ON tinderbox.conversations (privacy_class);
CREATE INDEX IF NOT EXISTS conversations_superseded_by_idx
    ON tinderbox.conversations (superseded_by_conversation_id)
    WHERE superseded_by_conversation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS conversations_raw_metadata_gin
    ON tinderbox.conversations USING gin (raw_metadata);

-- ============================================================================
-- messages — one row per turn within a conversation
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.messages (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id          uuid        NOT NULL REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    export_id                text,
    position                 int         NOT NULL,
    role                     text        NOT NULL
                             CHECK (role IN ('human', 'assistant', 'tool_use', 'tool_result', 'system')),
    content                  text        NOT NULL DEFAULT '',
    raw_content              jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at_source        timestamptz,
    model                    text,
    stop_reason              text,
    content_hash             text        GENERATED ALWAYS AS (encode(sha256(content::bytea), 'hex')) STORED,
    tsv                      tsvector    GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED,
    privacy_class_override   text        REFERENCES tinderbox.privacy_classes(slug)
);

COMMENT ON TABLE tinderbox.messages IS
  'One row per message turn. Insert-only — content is immutable once ingested (design principle #2). raw_content preserves original block array (tool_use, thinking, citations); content is rendered text for FTS.';

-- Idempotency: by export_id when present, by position as fallback.
CREATE UNIQUE INDEX IF NOT EXISTS messages_conv_export_uniq
    ON tinderbox.messages (conversation_id, export_id)
    WHERE export_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_conv_position_uniq
    ON tinderbox.messages (conversation_id, position);

CREATE INDEX IF NOT EXISTS messages_tsv_gin
    ON tinderbox.messages USING gin (tsv);
CREATE INDEX IF NOT EXISTS messages_role_idx
    ON tinderbox.messages (role);
CREATE INDEX IF NOT EXISTS messages_raw_content_gin
    ON tinderbox.messages USING gin (raw_content);

-- ============================================================================
-- artifacts — versioned. Every create/update/rewrite produces a new row.
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.artifacts (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid        NOT NULL REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    message_id          uuid        REFERENCES tinderbox.messages(id) ON DELETE SET NULL,
    artifact_id         text        NOT NULL,
    version             int         NOT NULL,
    command             text        NOT NULL
                        CHECK (command IN ('create', 'update', 'rewrite')),
    type                text,
    language            text,
    title               text,
    content             text        NOT NULL DEFAULT '',
    payload             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    content_hash        text        GENERATED ALWAYS AS (encode(sha256(content::bytea), 'hex')) STORED,
    tsv                 tsvector    GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))) STORED,
    created_at_source   timestamptz,
    UNIQUE (artifact_id, version)
);

COMMENT ON TABLE tinderbox.artifacts IS
  'Versioned artifacts. Insert-only. Latest version per artifact_id is the "current" artifact (convenience view to be added when query patterns demand it).';

CREATE INDEX IF NOT EXISTS artifacts_conversation_idx
    ON tinderbox.artifacts (conversation_id);
CREATE INDEX IF NOT EXISTS artifacts_artifact_version_idx
    ON tinderbox.artifacts (artifact_id, version DESC);
CREATE INDEX IF NOT EXISTS artifacts_tsv_gin
    ON tinderbox.artifacts USING gin (tsv);

-- ============================================================================
-- attachments — files uploaded into conversations
-- Binaries live on Mac Studio filesystem; blob_path is RELATIVE under config-defined root.
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.attachments (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid        NOT NULL REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    message_id          uuid        REFERENCES tinderbox.messages(id) ON DELETE SET NULL,
    filename            text,
    mime_type           text,
    size_bytes          bigint,
    file_hash           text        NOT NULL,
    blob_path           text        NOT NULL,
    extracted_text      text,
    payload             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    tsv                 tsvector    GENERATED ALWAYS AS (to_tsvector('english', coalesce(extracted_text, ''))) STORED,
    created_at_source   timestamptz,
    UNIQUE (conversation_id, file_hash)
);

COMMENT ON TABLE tinderbox.attachments IS
  'Attachments. Binary content lives on Mac Studio filesystem at <blob_root>/<conversation_export_id>/<file_hash>. blob_path is RELATIVE so the DB survives machine migration. Worker config holds the absolute root path.';

CREATE INDEX IF NOT EXISTS attachments_conversation_idx
    ON tinderbox.attachments (conversation_id);
CREATE INDEX IF NOT EXISTS attachments_file_hash_idx
    ON tinderbox.attachments (file_hash);
CREATE INDEX IF NOT EXISTS attachments_tsv_gin
    ON tinderbox.attachments USING gin (tsv);

-- ============================================================================
-- record this migration
-- ============================================================================
INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (2, '002_core_tables', 'PENDING_CHECKSUM')
ON CONFLICT (version) DO NOTHING;
