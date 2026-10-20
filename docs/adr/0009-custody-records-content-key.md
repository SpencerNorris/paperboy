# ADR-0009: Custody records the content key of each sighting

## Status

Accepted (2026-10-08, #91).

## Context

Telegram identifies a photo or document by an id (`photo:<id>`,
`document:<id>`) before a byte is downloaded; the media collector's
`content_key` uses it to skip a download of content the store already holds.
The index behind that skip was rebuilt from `media JOIN messages`: a file was
filed under the content key of its message's CURRENT `media_json`.

`fetch-from-list` (#91) re-fetches posts that are already stored, so a post can
be edited to a different photo between two runs. The index then filed the OLD
file under the NEW photo's key: the new content looked already held, was never
downloaded, and the report named the old file. A repost sighting (a second
message carrying a file stored under the first) also needs a row of its own to
say where and when the file appeared.

## Options considered

- **a. Derive the key at read time from the message's revisions.** Rejected:
  it joins four tables per lookup and still cannot say which revision a file
  was downloaded for.
- **b. A `content_key` column on `media`.** Rejected: `media` is one row per
  stored file/message; the question is per sighting, and `custody_log` already
  is the per-sighting table (ADR-0002).
- **c. A `content_key` column on `custody_log`, written with the sighting.**
  Chosen.

## Decision

Migration `0008_custody_content_key.sql` adds `custody_log.content_key`
(`photo:<id>` / `document:<id>`, nullable). The media collector writes it with
every custody row it records (download or duplicate). The dedup index is
`load_content_index`: `content_key -> (sha256, media key)` from custody rows;
when several sightings share a content key the newest sighting wins. Eligibility
for download is decided on the stored post AFTER the `posts` phase.

NULL means "content unknown": avatars (no message), and sightings where the key
cannot be recovered with certainty. The backfill sets a key only when BOTH the
sighting's own message AND the message its file was originally stored for
(`media.message_uri` of the sighting's sha) are stable on that same key (current
media and every revision agree). The second condition is needed because the old
index filed a file under its message's CURRENT media: a dedup sighting of a
stable post Q could point at a file downloaded for a different photo of a post
that was edited to Q's photo later, and stamping it would be wrong. A NULL sighting is simply
absent from the index, so its content is downloaded again - a redundant
download, never a missed one. `raw_records` is never modified.

## Consequences

- An edited post's new photo is downloaded; the old file's custody rows are
  untouched.
- Every same-channel repost sighting keeps its own custody row; only the
  download is skipped. A message already recorded for a content is not walked
  again, so a re-run adds no rows.
- **A cross-channel repost gets no custody row of its own** (operator decision;
  the media phase's index is per channel, and `fetch-from-list` reports the row
  `already_stored` with the holding file instead of walking it, before any size
  cap). The sighting is still recoverable: join `messages.media_json` to
  `custody_log.content_key`, i.e. `custody_log.content_key = CASE
  lower(json_extract(m.media_json,'$._')) WHEN 'messagemediaphoto' THEN 'photo:'
  || json_extract(m.media_json,'$.photo.id') WHEN 'messagemediadocument' THEN
  'document:' || json_extract(m.media_json,'$.document.id') END` lists every
  message that carries content a custody row recorded. Whether to record such a
  sighting properly is follow-up #95.
- The media phase's safety-net dedup (two photo ids, identical bytes) now leaves a
  `MediaDownload` receipt, so `reproject` reproduces its custody row.
- `reproject` reproduces the column (the replayed media phase writes it the
  same way).
- Stores older than 0008 gain the column on `Store.open` like any migration.

## Notes

`docs/features/fetch-from-list.md`, `docs/data-model.md` (`custody_log`), issue
#91; ADR-0002, ADR-0007.
