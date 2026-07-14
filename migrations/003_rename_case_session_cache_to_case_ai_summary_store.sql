-- Renames case_session_cache to case_ai_summary_store — clearer name
-- for what the table actually holds (the persisted ai_summary per case,
-- D.1 of the Data Persistence Spec), and matches core/case_store.py /
-- core/case_session_repository.py, which already talk about "the
-- ai_summary store" rather than a generic "session cache".
--
-- Guarded so this is safe to run on every container start, in any order
-- relative to 001/002, and any number of times:
--   - Fresh install: 001 creates case_ai_summary_store directly, so
--     case_session_cache never exists here — this block is a no-op.
--   - Existing database still on the old name: renames it once.
--   - Already renamed: case_session_cache no longer exists — no-op.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'case_session_cache'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'case_ai_summary_store'
    ) THEN
        ALTER TABLE case_session_cache RENAME TO case_ai_summary_store;
    END IF;
END $$;
