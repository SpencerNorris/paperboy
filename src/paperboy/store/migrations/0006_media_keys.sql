-- 0006_media_keys (#62, ADR-0007): media locations become profile-relative keys.
--
-- media.path and custody_log.path historically held str(path) as the run built
-- it: repo-relative ("data/<profile>/media/xx/<sha>.<ext>") or absolute,
-- depending on how the run was launched. The canonical form is
-- "media/<sha[:2]>/<sha><ext>", resolved against the profile dir at read time.
--
-- The rewrite anchors on the row's own sha256 (the filename is everything from
-- the sha onward), so it is correct for relative, absolute and Windows-style
-- values alike, and preserves extension case. It is idempotent: a row already
-- equal to its canonical key is not touched. Rows that do not contain their own
-- sha are left as they are and reported by Store.open (never guessed).
-- raw_records is not modified; replay normalises old payloads.

UPDATE media
SET path = 'media/' || substr(sha256, 1, 2) || '/' || substr(path, instr(path, sha256))
WHERE path IS NOT NULL
  AND instr(path, sha256) > 0
  AND path <> 'media/' || substr(sha256, 1, 2) || '/' || substr(path, instr(path, sha256));

UPDATE custody_log
SET path = 'media/' || substr(sha256, 1, 2) || '/' || substr(path, instr(path, sha256))
WHERE path IS NOT NULL
  AND instr(path, sha256) > 0
  AND path <> 'media/' || substr(sha256, 1, 2) || '/' || substr(path, instr(path, sha256));
