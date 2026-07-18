-- BSI Phase 2 — Modify Investigation Steps.
-- Adds investigation_plan_overrides per Data Persistence Spec v1.0,
-- Section D.6.
--
-- NATURE: primary fact, not a derived artifact. This is an
-- investigator's explicit override of the AI-generated
-- investigation_steps, made through the Investigation Plan "Modify"
-- popup. It is stored separately from case_ai_summary_store so that an
-- ai_ins_reload_ai_summary event — which force-overwrites the cached
-- ai_summary (Section E.2) — can never silently discard a saved edit.
-- investigation_plan_overrides is explicitly out of scope for that
-- reload sequence (Section E.5) and is read independently by both
-- /plan and /copilot so the override is always reflected, regardless
-- of which endpoint is queried or what the calling client passes.
--
-- SCOPE: overrides investigation_steps only. evidence_checklist,
-- escalation_criteria, fraud_types, risk_tier, and the narrative
-- summary remain AI-generated at all times and are never stored here.
--
-- RETENTION: durable, permanent. One row per case, current state
-- only — a new save overwrites the prior one via the UNIQUE
-- constraint below. No version history; if a future records-retention
-- determination extends to investigation plan edits, versioning can
-- be added additively.
CREATE TABLE IF NOT EXISTS investigation_plan_overrides (
    id              BIGSERIAL   PRIMARY KEY,
    case_id         TEXT        NOT NULL UNIQUE,
    modified_steps  JSONB       NOT NULL,
    modified_by     TEXT        NOT NULL,
    comment         TEXT,
    modified_on     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The UNIQUE constraint on case_id already gives us an index for the
-- point lookups /plan and /copilot perform on every call, but declare
-- it explicitly so it survives even if the UNIQUE constraint is ever
-- relaxed (e.g. if versioning is added later per the retention note
-- above).
CREATE INDEX IF NOT EXISTS idx_investigation_plan_overrides_case_id
    ON investigation_plan_overrides (case_id);