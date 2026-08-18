-- BSI Phase 2 — attribute every persisted response to the calling
-- investigator's username.
--
-- Every endpoint now accepts `username` (and `token`, the caller's
-- AppWorks SAML token) in its request body. `token` is deliberately
-- NEVER persisted anywhere — it identifies/authenticates the caller for
-- the duration of this one request only. `username` IS persisted,
-- alongside the response, in every PostgreSQL table that stores a
-- response — so this migration adds a nullable username column to each
-- of them.
--
-- Nullable (not NOT NULL): existing rows written before this change have
-- no username to backfill, and several writers (etl/run_sync.py's CLI
-- path, background reasoning triggered without an HTTP caller in scope)
-- have no request-scoped username at all. The API layer still requires
-- username on every request; this column simply also tolerates rows
-- written by a caller that legitimately has none.
--
-- investigation_plan_overrides already had `modified_by` (the
-- investigator_id field on that endpoint's own contract) before this
-- migration — `username` is added alongside it, not instead of it: the
-- two can differ (investigator_id is the BSI business identity recorded
-- for attribution on the override itself; username is the generic caller
-- identity every endpoint now carries), so neither replaces the other.

ALTER TABLE case_ai_summary_store
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE conversation_history
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE agent_audit_log
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE report_artifacts
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE pipeline_execution_state
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE graph_ingest_state
    ADD COLUMN IF NOT EXISTS username TEXT;

ALTER TABLE investigation_plan_overrides
    ADD COLUMN IF NOT EXISTS username TEXT;
