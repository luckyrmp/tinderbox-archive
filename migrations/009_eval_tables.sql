-- Migration 009 — Eval infrastructure (stage 2.5 / scheduled retrieval QA)
--
-- Two tables:
--   tinderbox.eval_queries — frozen test set, generated once, used across runs.
--   tinderbox.eval_runs    — one row per scheduled eval run, aggregate metrics.
--
-- Per-query results land in a JSONB column on eval_runs (not their own table)
-- because we want strict point-in-time snapshots tied to a single run, not
-- normalized history.

CREATE TABLE IF NOT EXISTS tinderbox.eval_queries (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text      text        NOT NULL,
    flavor          text        NOT NULL
                    CHECK (flavor IN ('vague_semantic', 'specific_factual', 'fragmentary')),
    -- The source conversation this query was generated from. Top-K is correct
    -- when this conversation appears in the result set.
    source_conversation_id  uuid    NOT NULL
                                    REFERENCES tinderbox.conversations(id) ON DELETE CASCADE,
    -- Optional: specific message or artifact this query was crafted to hit.
    -- If non-null, we also score whether the result top-K includes this exact item.
    target_message_id       uuid    REFERENCES tinderbox.messages(id) ON DELETE SET NULL,
    target_artifact_id      uuid    REFERENCES tinderbox.artifacts(id) ON DELETE SET NULL,
    generation_model        text,
    generation_payload      jsonb   NOT NULL DEFAULT '{}'::jsonb,
    is_active               boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tinderbox.eval_queries IS
  'Frozen retrieval eval set. Generated once via Haiku from sampled conversations. is_active=false retires queries without deleting them so historical eval_runs can still reference them.';

CREATE INDEX IF NOT EXISTS eval_queries_active_idx
    ON tinderbox.eval_queries (is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS eval_queries_source_idx
    ON tinderbox.eval_queries (source_conversation_id);


CREATE TABLE IF NOT EXISTS tinderbox.eval_runs (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    queries_run         int         NOT NULL DEFAULT 0,
    hit_at_1            int         NOT NULL DEFAULT 0,
    hit_at_3            int         NOT NULL DEFAULT 0,
    hit_at_10           int         NOT NULL DEFAULT 0,
    -- Per-query records. Each entry: {eval_query_id, query_text, flavor,
    --   source_conversation_id, returned_conversation_ids, hit_position (1-based, null if miss),
    --   latency_ms, top_score}
    per_query_results   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    config_snapshot     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    notes               text,
    UNIQUE (started_at)
);

COMMENT ON TABLE tinderbox.eval_runs IS
  'One row per scheduled eval run. Hit rates are aggregate; per-query detail is in per_query_results JSONB. config_snapshot captures hybrid_search params (cosine_filter, weight, etc.) so regressions tied to tuning are diagnosable.';

CREATE INDEX IF NOT EXISTS eval_runs_started_idx
    ON tinderbox.eval_runs (started_at DESC);


GRANT SELECT, INSERT, UPDATE, DELETE ON tinderbox.eval_queries, tinderbox.eval_runs TO service_role;

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (11, '009_eval_tables', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
