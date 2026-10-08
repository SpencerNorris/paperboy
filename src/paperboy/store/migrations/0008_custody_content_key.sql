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
-- Backfill: only where it is certain. A sighting gets its key when the message
-- has never carried any other content key (its current media_json and every
-- revision agree); an edited message's sightings stay NULL ("unknown"), which
-- makes the media phase fetch the content again (a redundant download, never a
-- missed one). Avatar sightings have no message and stay NULL. raw_records is
-- not modified.

ALTER TABLE custody_log ADD COLUMN content_key TEXT;

UPDATE custody_log
SET content_key = (
    SELECT CASE lower(json_extract(m.media_json, '$._'))
               WHEN 'messagemediaphoto'
                   THEN 'photo:' || json_extract(m.media_json, '$.photo.id')
               WHEN 'messagemediadocument'
                   THEN 'document:' || json_extract(m.media_json, '$.document.id')
           END
    FROM messages m
    WHERE m.uri = custody_log.source_message_uri
      AND m.media_json IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM message_revisions r
          WHERE r.message_uri = m.uri
            AND COALESCE(
                    CASE lower(json_extract(r.media_json, '$._'))
                        WHEN 'messagemediaphoto'
                            THEN 'photo:' || json_extract(r.media_json, '$.photo.id')
                        WHEN 'messagemediadocument'
                            THEN 'document:' || json_extract(r.media_json, '$.document.id')
                    END, '') <>
                COALESCE(
                    CASE lower(json_extract(m.media_json, '$._'))
                        WHEN 'messagemediaphoto'
                            THEN 'photo:' || json_extract(m.media_json, '$.photo.id')
                        WHEN 'messagemediadocument'
                            THEN 'document:' || json_extract(m.media_json, '$.document.id')
                    END, '')
      )
)
WHERE source_message_uri IS NOT NULL;
