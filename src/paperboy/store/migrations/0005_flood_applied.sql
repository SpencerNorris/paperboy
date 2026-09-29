-- 0005_flood_applied: the wait Budget actually applied for a FLOOD_WAIT (#69).
-- `seconds` stays the server's number; `applied_seconds` = ceil(seconds*1.1)+5,
-- and the persisted cooldown (`until`) is based on it. Nullable: rows written
-- before #69 have no applied value and are never rewritten.
ALTER TABLE flood_log ADD COLUMN applied_seconds INTEGER;
