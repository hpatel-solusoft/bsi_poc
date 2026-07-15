-- BSI Phase 2 — completes Phase 1 Postgres Foundation (project plan 1.1),
-- which listed five tables; only three were built in the earlier round.
-- Adds the remaining two, per Data Persistence Spec v1.0 Sections D.3/D.5.

-- D.3 pipeline_execution_state
-- Pure orchestration state — closes the gap the Reasoning Pipeline spec
-- (Principles 10/15) left unimplemented: "runs once per subject per
-- case, does not re-run unless explicitly cleared" cannot actually be
-- enforced without a persisted record of what has already run.
--
-- WAVE MEMBERSHIP — CONFIRMED SOURCE OF TRUTH: the Python Implementation
-- Reference (BSI_Phase2_Python_Implementation_Reference_0_1.md) governs
-- wave membership wherever it conflicts with another Phase 2 document.
-- Confirmed directly. Wave 1 is rules {1,3,5,10,11} and Wave 2 is rules
-- {2,4,6,7,8,9,12,13} by explicit membership list (Section 5.4) — NOT
-- the numeric range ("Rules 01 to 08" / "Rules 09 to 14") the Data
-- Persistence Spec's own column comments use; Rules 7, 8, 10, 11 all
-- fall outside a clean range split, which is exactly what Section 5.4
-- warns against. The column names (wave1_status, wave2_status) are
-- unchanged from the Data Persistence Spec — only its range-based
-- description was stale, not the column shape.
CREATE TABLE IF NOT EXISTS pipeline_execution_state (
    case_id              TEXT        NOT NULL,
    subject_id           TEXT        NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'completed', 'failed')),
    wave1_status         TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (wave1_status IN ('pending', 'complete')),
    wave1_completed_at   TIMESTAMPTZ,
    wave2_status         TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (wave2_status IN ('pending', 'complete')),
    wave2_completed_at   TIMESTAMPTZ,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ,
    failed_at            TIMESTAMPTZ,
    cleared_at           TIMESTAMPTZ,
    cleared_reason       TEXT,
    PRIMARY KEY (case_id, subject_id)
);

-- D.5 report_artifacts
-- Working/draft copy only — the AppWorks-saved report (via the native
-- BSI UI save action, Principle 9) is always the authoritative version.
-- This table exists so a report can be regenerated, compared across
-- drafts, or recovered without re-running the full agent chain.
CREATE TABLE IF NOT EXISTS report_artifacts (
    id            BIGSERIAL PRIMARY KEY,
    case_id       TEXT        NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content       JSONB       NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'saved_to_appworks'))
);
CREATE INDEX IF NOT EXISTS idx_report_artifacts_case_id
    ON report_artifacts (case_id, generated_at);
