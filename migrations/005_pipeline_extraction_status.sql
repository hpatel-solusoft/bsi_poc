-- BSI Phase 2 — Phase 5 (Narrative Extraction Stage) build.
--
-- pipeline_execution_state (D.3) originally tracked only wave1_status
-- and wave2_status. That was accurate when Wave 2 (Rules 2,4,6,7,8,9,
-- 12,13) and its prerequisite Narrative Extraction + Graph Load steps
-- (Python Implementation Reference, Section 5.3 Steps 3-4) were both
-- entirely unbuilt and tracked as one "not yet implemented" gap.
--
-- Now that Steps 3-4 are built (this migration's companion code:
-- reasoning_layer/commentary_reader.py, reasoning_layer/extraction_stage.py,
-- reasoning_layer/graph_load.py) but Wave 2 rule execution (Step 5,
-- project plan Phase 6) is not, collapsing both into wave2_status would
-- make it impossible to tell "extraction ran and wrote attributions"
-- apart from "extraction hasn't run yet" once Wave 2 is added later and
-- wave2_status starts getting set for real. A dedicated column avoids
-- overloading wave2_status with two different stages' completion state.
--
-- Deliberately NOT touching wave1_status/wave2_status themselves —
-- Rule 1 in migration 004's own comment block already confirmed the
-- Python Implementation Reference's membership-list wave definition as
-- authoritative; this migration only adds a new column, it does not
-- reinterpret the existing ones.

ALTER TABLE pipeline_execution_state
    ADD COLUMN IF NOT EXISTS extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN ('pending', 'complete')),
    ADD COLUMN IF NOT EXISTS extraction_completed_at TIMESTAMPTZ;
