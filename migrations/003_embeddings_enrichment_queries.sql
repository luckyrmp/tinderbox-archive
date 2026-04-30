-- Migration 003 — embeddings + enrichment + named_instances + queries log
-- pgvector 0.8.0 confirmed; hnsw index AM available — using hnsw (vector_cosine_ops).
-- Embedding model locked: mxbai-embed-large (1024d), manifest digest 468836162de7.
-- Idempotent: yes.

-- ============================================================================
-- embeddings — vectors for messages, artifact chunks, enrichment summaries, attachment text
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.embeddings (
    id                uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type       text          NOT NULL
                      CHECK (source_type IN ('message', 'artifact', 'enrichment_summary', 'attachment_text')),
    source_id         uuid          NOT NULL,
    chunk_index       int           NOT NULL DEFAULT 0,
    chunk_text        text          NOT NULL,
    embedding         vector(1024)  NOT NULL,
    embedding_model   text          NOT NULL DEFAULT 'mxbai-embed-large',
    embedding_dim     int           NOT NULL DEFAULT 1024,
    generated_at      timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, chunk_index, embedding_model)
);

COMMENT ON TABLE tinderbox.embeddings IS
  'Polymorphic source via (source_type, source_id). FK integrity enforced application-side because Postgres does not support polymorphic FKs natively. chunk_text is stored verbatim for explainability — what got fed to the model.';

-- hnsw index using cosine distance.
-- Defaults (m=16, ef_construction=64) are appropriate for archival-scale corpora.
CREATE INDEX IF NOT EXISTS embeddings_embedding_hnsw
    ON tinderbox.embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS embeddings_source_idx
    ON tinderbox.embeddings (source_type, source_id);

-- ============================================================================
-- enrichment — Haiku-generated annotations (NEVER overwrites source content)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.enrichment (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid        NOT NULL REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    summary             text,
    project_tags        text[]      NOT NULL DEFAULT '{}',
    client_tags         text[]      NOT NULL DEFAULT '{}',
    key_decisions       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    topics              text[]      NOT NULL DEFAULT '{}',
    enriched_at         timestamptz NOT NULL DEFAULT now(),
    enrichment_model    text        NOT NULL,
    enrichment_version  int         NOT NULL DEFAULT 1,
    payload             jsonb       NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE tinderbox.enrichment IS
  'Annotations only. Never returned as primary results. Stage 5 MCP layer renders these with different visual treatment than original messages. Append-only history per conversation — re-running enrichment inserts a new row. Latest = MAX(enriched_at) per conversation_id.';

CREATE INDEX IF NOT EXISTS enrichment_conversation_enriched_idx
    ON tinderbox.enrichment (conversation_id, enriched_at DESC);
CREATE INDEX IF NOT EXISTS enrichment_project_tags_gin
    ON tinderbox.enrichment USING gin (project_tags);
CREATE INDEX IF NOT EXISTS enrichment_client_tags_gin
    ON tinderbox.enrichment USING gin (client_tags);

-- ============================================================================
-- named_instances — detected named Claudes (Reef, Wake, Song, Kuro, etc.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.named_instances (
    id                          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                        text        NOT NULL UNIQUE,
    aliases                     text[]      NOT NULL DEFAULT '{}',
    description                 text,
    first_seen_conversation_id  uuid        REFERENCES tinderbox.conversations(id) ON DELETE SET NULL,
    first_seen_at               timestamptz,
    payload                     jsonb       NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE tinderbox.named_instances IS
  'Named Claude instances Lucky has worked with. Populated by stage 4 enrichment.';

-- ============================================================================
-- conversation_named_instances — join table
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.conversation_named_instances (
    conversation_id     uuid          NOT NULL REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    named_instance_id   uuid          NOT NULL REFERENCES tinderbox.named_instances(id) ON DELETE CASCADE,
    confidence          numeric(3,2)  NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence            text,
    detected_at         timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, named_instance_id)
);

COMMENT ON TABLE tinderbox.conversation_named_instances IS
  'Which named instances appeared in which conversations. Stage 4 enrichment populates this; schema lives here from stage 1 to avoid a later migration.';

-- ============================================================================
-- queries — audit log of MCP read queries (written by stage 5; schema'd now)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tinderbox.queries (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    caller_identity          text        NOT NULL,
    query_text               text        NOT NULL,
    query_kind               text        NOT NULL
                             CHECK (query_kind IN ('semantic', 'keyword', 'provenance_lookup', 'hybrid', 'mcp_tool_call')),
    allowed_classes          text[]      NOT NULL DEFAULT '{}',
    intimate_unlocked        boolean     NOT NULL DEFAULT false,
    result_conversation_ids  uuid[]      NOT NULL DEFAULT '{}',
    result_message_ids       uuid[]      NOT NULL DEFAULT '{}',
    result_count             int         NOT NULL DEFAULT 0,
    latency_ms               int,
    useful                   boolean,
    payload                  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at               timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tinderbox.queries IS
  'Stage 5 writes here on every MCP read. Two purposes: (1) learn what Tinderbox is actually used for, (2) honestly evaluate whether enrichment-aware retrieval improves usefulness over baseline.';

CREATE INDEX IF NOT EXISTS queries_caller_created_idx
    ON tinderbox.queries (caller_identity, created_at DESC);
CREATE INDEX IF NOT EXISTS queries_intimate_idx
    ON tinderbox.queries (intimate_unlocked)
    WHERE intimate_unlocked = true;
CREATE INDEX IF NOT EXISTS queries_result_conversations_gin
    ON tinderbox.queries USING gin (result_conversation_ids);

-- ============================================================================
-- record this migration
-- ============================================================================
INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (3, '003_embeddings_enrichment_queries', 'PENDING_CHECKSUM')
ON CONFLICT (version) DO NOTHING;
