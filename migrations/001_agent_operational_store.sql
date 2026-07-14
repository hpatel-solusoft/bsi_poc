-- BSI Phase 2 — Agent Operational Store
-- Implements Section D of the Data Persistence and Synchronisation
-- Specification v1.0. Every table here is a derived artifact or pure
-- agent machinery — never the sole copy of a primary case fact
-- (Section A.1). AppWorks and Neo4j remain the systems of record.

-- D.1 case_ai_summary_store
-- Replaces CS-4 in-process CASE_STORE and the Phase 1 AppWorks
-- ai_summary field. Safe to lose and regenerate from AppWorks/Neo4j.
CREATE TABLE IF NOT EXISTS case_ai_summary_store (
    case_id            TEXT PRIMARY KEY,
    ai_summary         JSONB       NOT NULL,
    provenance_trail   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    source             TEXT        NOT NULL DEFAULT 'appworks_fetch',
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- D.2 conversation_history
-- Server-owned Copilot transcript. Supersedes the frontend-only
-- conversation_history principle. Rolling window of 20 turns per case
-- is enforced at the application layer (see core/conversation_repository.py).
CREATE TABLE IF NOT EXISTS conversation_history (
    id             BIGSERIAL PRIMARY KEY,
    case_id        TEXT        NOT NULL,
    turn_index     INTEGER     NOT NULL,
    role           TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content        TEXT        NOT NULL,
    sources_cited  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversation_history_case_id
    ON conversation_history (case_id, turn_index);

-- D.4 agent_audit_log
-- Pure operational telemetry. Append-only, standard log retention.
CREATE TABLE IF NOT EXISTS agent_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    case_id      TEXT        NOT NULL,
    agent_name   TEXT        NOT NULL,
    endpoint     TEXT        NOT NULL,
    latency_ms   INTEGER     NOT NULL,
    tokens_used  INTEGER,
    status       TEXT        NOT NULL CHECK (status IN ('success', 'error')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_log_case_id
    ON agent_audit_log (case_id, created_at);
