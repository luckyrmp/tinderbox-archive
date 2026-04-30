-- Migration 008 — Hybrid retrieval SQL function (stage 2)
--
-- Combines pgvector cosine distance with tsvector full-text rank into a
-- single server-side query. Returns top-N hits with provenance (conversation
-- title, message snippet) so the client can render results without follow-ups.
--
-- Score: (1 - cosine_distance) + 0.5 * fts_rank
--   - cosine in [0,2], so (1 - cos) is in [-1,1] but typically [0,1] for
--     positively-oriented embeddings.
--   - ts_rank_cd typically in [0,1] for short queries.
--   - 0.5 weight on FTS keeps semantic similarity primary, lexical as tiebreaker.
-- Tunable from the eval phase later.

CREATE OR REPLACE FUNCTION tinderbox.hybrid_search(
    query_embedding vector(1024),
    query_text text,
    result_limit int DEFAULT 10,
    cosine_filter float DEFAULT 0.6
)
RETURNS TABLE (
    embedding_id uuid,
    source_type text,
    source_id uuid,
    chunk_index int,
    chunk_text text,
    cosine_sim float,
    fts_rank float,
    combined_score float,
    conversation_id uuid,
    conversation_export_id text,
    conversation_title text,
    message_position int,
    artifact_title text
)
LANGUAGE sql
STABLE
AS $$
    WITH q AS (
        SELECT plainto_tsquery('english', query_text) AS tsq
    ),
    candidates AS (
        SELECT
            e.id AS embedding_id,
            e.source_type,
            e.source_id,
            e.chunk_index,
            e.chunk_text,
            (1 - (e.embedding <=> query_embedding))::float AS cosine_sim
        FROM tinderbox.embeddings e
        WHERE (e.embedding <=> query_embedding) < cosine_filter
        ORDER BY e.embedding <=> query_embedding
        LIMIT result_limit * 4   -- coarse pool for re-ranking
    ),
    msg_join AS (
        SELECT
            c.embedding_id,
            c.source_type,
            c.source_id,
            c.chunk_index,
            c.chunk_text,
            c.cosine_sim,
            m.conversation_id AS m_conversation_id,
            m.position AS m_position,
            m.tsv AS m_tsv,
            COALESCE(ts_rank_cd(m.tsv, q.tsq), 0)::float AS m_fts
        FROM candidates c
        CROSS JOIN q
        LEFT JOIN tinderbox.messages m
          ON c.source_type = 'message' AND c.source_id = m.id
    ),
    art_join AS (
        SELECT
            mj.embedding_id,
            mj.source_type,
            mj.source_id,
            mj.chunk_index,
            mj.chunk_text,
            mj.cosine_sim,
            mj.m_conversation_id,
            mj.m_position,
            mj.m_fts,
            a.conversation_id AS a_conversation_id,
            a.title AS a_title,
            COALESCE(ts_rank_cd(a.tsv, q.tsq), 0)::float AS a_fts
        FROM msg_join mj
        CROSS JOIN q
        LEFT JOIN tinderbox.artifacts a
          ON mj.source_type = 'artifact' AND mj.source_id = a.id
    ),
    scored AS (
        SELECT
            embedding_id,
            source_type,
            source_id,
            chunk_index,
            chunk_text,
            cosine_sim,
            CASE
                WHEN source_type = 'message' THEN m_fts
                WHEN source_type = 'artifact' THEN a_fts
                ELSE 0::float
            END AS fts_rank,
            COALESCE(m_conversation_id, a_conversation_id) AS conversation_id,
            m_position,
            a_title
        FROM art_join
    ),
    combined AS (
        SELECT
            *,
            (cosine_sim + 0.5 * fts_rank)::float AS combined_score
        FROM scored
    )
    SELECT
        c.embedding_id,
        c.source_type,
        c.source_id,
        c.chunk_index,
        c.chunk_text,
        c.cosine_sim,
        c.fts_rank,
        c.combined_score,
        c.conversation_id,
        conv.export_id AS conversation_export_id,
        conv.title AS conversation_title,
        c.m_position AS message_position,
        c.a_title AS artifact_title
    FROM combined c
    LEFT JOIN tinderbox.conversations conv ON conv.id = c.conversation_id
    ORDER BY c.combined_score DESC
    LIMIT result_limit;
$$;

-- Grant execution to service_role (stage 1 expedient) and tinderbox_owner.
-- service_role doesn't need this for its own queries (it's superuser-adjacent),
-- but PostgREST routes RPC through the calling role, so we grant explicitly.
GRANT EXECUTE ON FUNCTION tinderbox.hybrid_search(vector(1024), text, int, float)
    TO service_role, tinderbox_owner;

INSERT INTO tinderbox.schema_migrations (version, name, checksum)
VALUES (10, '008_hybrid_search_function', 'inline_no_file')
ON CONFLICT (version) DO NOTHING;
