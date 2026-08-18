-- BSI Phase 2 — attribute every persisted response to the calling
-- investigator's username.
--
-- Every endpoint now accepts `username` (via the X-BSI-Username header)
-- and `token` (via Authorization: Bearer — the caller's AppWorks SAML
-- artifact; see api/auth_headers.py). `token` is deliberately NEVER
-- persisted anywhere — it identifies/authenticates the caller for the
-- duration of this one request only. `username` IS persisted, alongside
-- the response, in every PostgreSQL table that stores a response EXCEPT
-- case_ai_summary_store — this migration adds a nullable username
-- column to each of the other six.
--
-- case_ai_summary_store is the deliberate exception: it holds ONE row
-- per case_id, but a case's ai_summary blob has up to four
-- independently-attributed entries (agent_summary_cache[route], one
-- per route — intake, similar_cases, risk_assessment, plan). A single
-- case-wide "who wrote this" column cannot represent that — it would
-- silently collapse four different answers into one, and be wrong for
-- at least three of them on any case with more than one tab generated.
-- Per-route attribution instead lives INSIDE the ai_summary JSON itself
-- (core.case_store.merge_agent_summary_cache writes
-- {route: {summary, generated_at, username}} — username sits right
-- alongside the generated_at every route's response already surfaces),
-- so it needs no column here at all. See
-- core/case_session_repository.py's upsert_case_session for the same
-- reasoning in code.
--
-- Nullable (not NOT NULL) on the six tables that DO get the column:
-- existing rows written before this change have no username to
-- backfill, and several writers (etl/run_sync.py's CLI path, background
-- reasoning triggered without an HTTP caller in scope) have no
-- request-scoped username at all. The API layer still requires
-- username on every request; this column simply also tolerates rows
-- written by a caller that legitimately has none.
--
-- investigation_plan_overrides already had `modified_by` (the
-- investigator_id field on that endpoint's own contract) before this
-- migration — `username` is added alongside it, not instead of it: the
-- two can differ (investigator_id is the BSI business identity recorded
-- for attribution on the override itself; username is the generic caller
-- identity every endpoint now carries), so neither replaces the other.

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
