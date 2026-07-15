-- BSI Phase 2 — ETL ingest tracking.
--
-- One row per case, recording what the AppWorks -> Neo4j ETL last did to
-- it. Pure operational machinery: fully regenerable by re-running the
-- ingest, never the sole copy of any case fact, so it passes the Data
-- Persistence Spec's Section A.1 test for what may live in PostgreSQL.
-- It deliberately does NOT live in Neo4j — Section C.2 reserves that
-- store for the reasoning graph and explicitly excludes pipeline
-- execution tracking, and this is the same class of thing.
--
-- Answers the question that becomes operationally load-bearing the
-- moment AppWorks starts driving ingest by lifecycle event: which cases
-- are actually in the graph, and did the last sync of case X succeed?
-- Without it, that is a log-grep.

CREATE TABLE IF NOT EXISTS graph_ingest_state (
    case_id      TEXT        PRIMARY KEY,
    status       TEXT        NOT NULL DEFAULT 'loading'
                 CHECK (status IN ('loading', 'loaded', 'reasoned', 'failed')),
    attempts     INTEGER     NOT NULL DEFAULT 0,
    -- Per-entity write counts from the last successful load (subjects,
    -- allegations, employers, wage_records, commentary, ...). Kept as
    -- JSONB rather than a column each, because the set of entity types
    -- the ETL loads will grow — :Asset is already modelled and waiting on
    -- a relationship type (see etl/GAP_ANALYSIS.md) — and each addition
    -- should not be a migration.
    counts       JSONB,
    started_at   TIMESTAMPTZ,
    loaded_at    TIMESTAMPTZ,
    reasoned_at  TIMESTAMPTZ,
    failed_at    TIMESTAMPTZ,
    last_error   TEXT
);

CREATE INDEX IF NOT EXISTS idx_graph_ingest_state_status
    ON graph_ingest_state (status);
