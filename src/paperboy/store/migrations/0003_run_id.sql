-- 0003_run_id: which collect pass produced each raw record (ADR-0005).
-- Nullable: legacy rows predate the column and are segmented at replay
-- time by the tier='self' marker rule — never rewritten here.
ALTER TABLE raw_records ADD COLUMN run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_raw_records_run ON raw_records(run_id);
