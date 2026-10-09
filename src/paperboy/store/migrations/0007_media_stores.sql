-- 0007_media_stores (#63, ADR-0008): custody_log records which media store a
-- sighting's bytes live in.
--
-- 'local' = the profile folder; a bucket store is its full gs://<bucket>/<prefix>
-- URL. Never an absolute path (the profile folder must stay movable, ADR-0007).
-- Every pre-existing row was written by a local run, which the default backfills.
-- media.path stays the store-neutral key; raw_records is not modified.

ALTER TABLE custody_log ADD COLUMN store TEXT NOT NULL DEFAULT 'local';
