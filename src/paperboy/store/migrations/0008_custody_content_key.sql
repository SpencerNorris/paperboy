-- 0008_custody_content_key (#91): custody_log records WHICH content a sighting
-- was for.
--
-- Telegram's photo/document id (`photo:<id>` / `document:<id>`, the media
-- collector's `content_key`) identifies a file before it is downloaded. Until
-- now the dedup index was rebuilt by joining a file to its message's CURRENT
-- media_json - wrong once a post is edited to a different photo: the new
-- content then looked like it was already held under the OLD file, and was
-- never downloaded. The key is now stored with the sighting that produced it.
--
-- Backfill: only where it is certain. A sighting gets its key when BOTH
--   * its own message has never carried any other content key (its current
--     media_json and every revision agree), AND
--   * the message the file was originally stored for (`media.message_uri` of
--     the sighting's sha) is stable on that same key.
-- The second condition matters: the old index filed a file under the CURRENT
-- media of its message, so a dedup sighting of message Q (stable on photo 2002)
-- can point at a file that was in fact downloaded for photo 1001 of a post
-- that was edited to 2002 later; stamping it `photo:2002` would be wrong.
-- Everything else stays NULL ("unknown"), which makes the media phase fetch the
-- content again (a redundant download, never a missed one): an edited
-- message's sightings, avatar sightings (no message), sightings whose file has
-- no media row. raw_records is not modified.

ALTER TABLE custody_log ADD COLUMN content_key TEXT;

CREATE TEMP TABLE _msg_key AS
SELECT m.uri AS uri,
       CASE lower(json_extract(m.media_json, '$._'))
           WHEN 'messagemediaphoto'
               THEN 'photo:' || json_extract(m.media_json, '$.photo.id')
           WHEN 'messagemediadocument'
               THEN 'document:' || json_extract(m.media_json, '$.document.id')
       END AS key
FROM messages m;

CREATE TEMP TABLE _rev_key AS
SELECT r.message_uri AS uri,
       CASE lower(json_extract(r.media_json, '$._'))
           WHEN 'messagemediaphoto'
               THEN 'photo:' || json_extract(r.media_json, '$.photo.id')
           WHEN 'messagemediadocument'
               THEN 'document:' || json_extract(r.media_json, '$.document.id')
       END AS key
FROM message_revisions r;

-- Messages that never carried a content key other than their current one.
CREATE TEMP TABLE _stable_key AS
SELECT k.uri AS uri, k.key AS key
FROM _msg_key k
WHERE k.key IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM _rev_key r WHERE r.uri = k.uri AND COALESCE(r.key, '') <> k.key
  );

UPDATE custody_log
SET content_key = (
    SELECT s.key
    FROM _stable_key s
    WHERE s.uri = custody_log.source_message_uri
      AND EXISTS (
          SELECT 1
          FROM media md JOIN _stable_key o ON o.uri = md.message_uri
          WHERE md.sha256 = custody_log.sha256 AND o.key = s.key
      )
)
WHERE source_message_uri IS NOT NULL;

DROP TABLE _stable_key;
DROP TABLE _rev_key;
DROP TABLE _msg_key;
