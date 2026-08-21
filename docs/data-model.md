# Data model / codebook

The complete reference for the SQLite database paperboy writes to
`<data_dir>/<profile>/paperboy.sqlite` (default `./data/default/paperboy.sqlite`).
The authoritative schema is `src/paperboy/store/migrations/0001_init.sql`; this
document explains what each table and column means.

For a quick orientation and how to browse the data (Datasette, `sqlite3`), see
the **"What's in the database"** section of the [README](../README.md).

## Cross-cutting conventions

- **URI ids.** Entities are identified by string URIs, not bare integers:
  `tg:channel:<id>`, `tg:chat:<id>`, `tg:user:<id>`, and messages
  `tg:msg:<channel_id>/<msg_id>`. This makes ids stable, self-describing, and
  ready for graph/RDF export.
- **Timestamps** are ISO-8601 UTC text (e.g. `2026-08-21T14:30:51.540418+00:00`).
- **`*_json` columns** hold verbatim JSON projections of the corresponding
  Telegram (TL) object or sub-object — kept so no field is lost even before the
  normalized columns understand it.
- **`source_raw_id`** on a projected row points back to the `raw_records` row it
  was derived from (provenance; lets any projection be rebuilt from raw).
- **`tier`** records the visibility level a fact was observed at:
  `stranger` | `member` | `contact` | `admin` | `self`. The same field can look
  different at different tiers, so the tier is part of the record.
- **`first_seen` / `last_seen`** on entity tables bound when paperboy first and
  most recently saw that row — `last_seen` advances on every re-observation even
  when nothing changed.
- **Raw first.** Every TL object is written to `raw_records` *before* it is
  projected into the normalized tables. `raw_records` is the system of record; a
  Telegram schema change can be re-parsed from it rather than re-scraped.
- **History is append-only.** Edits, counter changes, and deletions are new rows
  in `message_revisions` / `message_metrics` / `channel_snapshots` /
  `message_tombstones`, never overwrites — so the timeline is preserved.

---

## `raw_records` — system of record

Every Telegram object exactly as received, before any normalization.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id; referenced by every projection's `source_raw_id`. |
| `kind` | TEXT | TL type name (e.g. `channelFull`, `message`, `user`). |
| `observed_at` | TEXT | When paperboy received it (UTC). |
| `tier` | TEXT | Visibility tier at capture (see conventions). |
| `context_json` | TEXT | Optional capture context (e.g. resolve target, request params). |
| `payload_json` | TEXT | The full TL object as JSON (`to_dict()`). |

## Entities (current state)

### `channels` — the channel/supergroup itself

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Telegram channel id (no `-100` prefix). |
| `uri` | TEXT UNIQUE | `tg:channel:<id>`. |
| `username` | TEXT | Public @username, or NULL if private/none. |
| `title` | TEXT | Display title. |
| `about` | TEXT | Description / bio text. |
| `kind` | TEXT | `broadcast`, `megagroup`, `gigagroup`, or `forum`. |
| `created_at` | TEXT | Channel creation date, when exposed. |
| `linked_chat_id` | INTEGER | Linked discussion group id (0/NULL if none). |
| `participants_count` | INTEGER | Subscriber/member count at last observation. |
| `flags_json` | TEXT | Boolean flags (verified, scam, fake, restricted, `participants_hidden`, `join_to_send`, …). |
| `restriction_json` | TEXT | `restriction_reason[]` (platform + reason: porno/terms/sensitive), if restricted. |
| `source_raw_id` | INTEGER | Provenance → `raw_records`. |
| `first_seen` / `last_seen` | TEXT | Bounds of observation. |

### `peers` — any entity id encountered, with `min`-provenance

Generic projection for **any** peer (`user`, `channel`, `chat`) seen anywhere —
message authors, forwarders, mentions, etc. Channel-typed peers also get a
richer row in `channels`.

| Column | Type | Meaning |
|---|---|---|
| `uri` | TEXT PK | `tg:user:<id>` / `tg:channel:<id>` / `tg:chat:<id>`. |
| `kind` | TEXT | `user`, `channel`, or `chat`. |
| `id` | INTEGER | Telegram id. |
| `access_hash` | INTEGER | Per-session access hash (may be NULL/min). |
| `is_min` | INTEGER | 1 if only a `min` constructor was seen (reduced fields; hash usable only for profile-photo fetch). |
| `seen_in_chat` | INTEGER | Channel id this peer was seen in (min-provenance). |
| `seen_in_msg` | INTEGER | Message id this peer was seen in — needed to build `inputUserFromMessage` for a min peer. |
| `username` | TEXT | @username if known. |
| `first_name` / `last_name` | TEXT | For user-typed peers. |
| `title` | TEXT | For channel/chat-typed peers. |
| `flags_json` | TEXT | Boolean flags (bot, verified, scam, fake, premium, …). |
| `source_raw_id` | INTEGER | Provenance. |
| `first_seen` / `last_seen` | TEXT | Bounds of observation. |

### `messages` — current state of every message

| Column | Type | Meaning |
|---|---|---|
| `uri` | TEXT PK | `tg:msg:<channel_id>/<msg_id>`. |
| `channel_id` | INTEGER | Owning channel id. |
| `msg_id` | INTEGER | Per-channel sequential message id. |
| `date` | TEXT | Original post time (UTC). |
| `edit_date` | TEXT | Last edit time, or NULL if never edited. |
| `from_uri` | TEXT | Author peer URI (channel peer for anonymous admins; NULL for some). |
| `post_author` | TEXT | Signature string, when the channel signs posts. |
| `text` | TEXT | Message text (empty string for media-only/service messages). |
| `entities_json` | TEXT | Formatting/entities (mentions, links, custom emoji). |
| `media_kind` | TEXT | `photo`, `document`, `webpage`, `poll`, `geo`, … or NULL. |
| `media_json` | TEXT | The media object as JSON. |
| `fwd_json` | TEXT | Forward header (origin peer/name, channel_post, date) if forwarded. |
| `reply_to_msg_id` | INTEGER | Message this replies to. |
| `reply_to_top_id` | INTEGER | Thread root (comment threads / forum topics). |
| `grouped_id` | INTEGER | Album group id (media groups share one). |
| `via_bot_id` | INTEGER | Inline bot the message was sent via. |
| `is_service` | INTEGER | 1 for service messages (joins, pins, title changes, …). |
| `action_json` | TEXT | The service action object, when `is_service=1`. |
| `content_hash` | TEXT | Hash of text+media; a change triggers a new `message_revisions` row. |
| `deleted_at` | TEXT | Set when a deletion is observed via `update`/`empty` evidence (see `message_tombstones`); NULL otherwise. |
| `source_raw_id` | INTEGER | Provenance. |
| `first_seen` / `last_seen` | TEXT | Bounds of observation. |

Unique index on `(channel_id, msg_id)`.

### `media` — downloaded files (Phase 2)

Content-addressed by SHA-256; deduped across messages.

| Column | Type | Meaning |
|---|---|---|
| `sha256` | TEXT PK | File hash (dedup key). |
| `message_uri` | TEXT | Message the file came from. |
| `kind` | TEXT | `photo` (server-re-encoded) or `document` (byte-exact). |
| `mime_type` | TEXT | MIME type (documents). |
| `size` | INTEGER | Byte size. |
| `file_name` | TEXT | Original filename (documents). |
| `attributes_json` | TEXT | Video/audio/sticker attributes. |
| `path` | TEXT | Where the file was written on disk. |
| `downloaded_at` | TEXT | Download time. |
| `exif_json` | TEXT | Extracted EXIF/metadata (documents). |

## History (append-only)

### `channel_snapshots` — the channel over time

One row per observation; diff them to see subscriber growth, renames, etc.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `channel_id` | INTEGER | Channel. |
| `observed_at` | TEXT | Snapshot time. |
| `participants_count` | INTEGER | Subscriber/member count then. |
| `online_count` | INTEGER | Online count then, when available. |
| `title` / `username` | TEXT | Title/username then (catches renames). |
| `about_hash` | TEXT | Hash of the description (detects about-text changes cheaply). |
| `source_raw_id` | INTEGER | Provenance. |

### `message_revisions` — edit history

Append-only; one row per observed version of a message (including the first).
A new row is written whenever `content_hash` changes.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `message_uri` | TEXT | The message. |
| `observed_at` | TEXT | When this version was seen. |
| `edit_date` | TEXT | Telegram's edit timestamp for this version. |
| `content_hash` | TEXT | Hash of this version. |
| `text` | TEXT | Text of this version. |
| `entities_json` / `media_json` | TEXT | Entities/media of this version. |
| `source_raw_id` | INTEGER | Provenance. |

> Telegram serves only the *current* version of a message; edit history exists
> **only** because paperboy snapshots each version it observes. Versions posted
> before you first collected are not recoverable.

### `message_metrics` — counter time series

Append-only; views/forwards/replies/reactions per observation. These counters
carry no `pts`, so they can only be captured by snapshotting.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `message_uri` | TEXT | The message. |
| `observed_at` | TEXT | Observation time. |
| `views` | INTEGER | View count then. |
| `forwards` | INTEGER | Forward count then. |
| `replies` | INTEGER | Comment/reply count then. |
| `reactions_json` | TEXT | Reaction tallies then. |

### `message_tombstones` — deletions

Records that a message id no longer exists, ranked by how sure we are. No FK to
`messages`, because a deleted id may never have been observed as a stored
message.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `message_uri` | TEXT | The (now-deleted) message URI. |
| `observed_at` | TEXT | When the deletion was detected. |
| `evidence` | TEXT | One of: **`update`** (a live delete event — unambiguous), **`empty`** (`messageEmpty` returned for an id inside a verified-complete range — strong), **`gap`** (id never seen; may be a hidden/service message — weak). `messages.deleted_at` is set only for `update`/`empty`. |

## Graph

### `edges` — triple-shaped relationships

`(subject) —predicate→ (object)`, each with provenance. Ready for GraphML/RDF
export.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `subject_uri` | TEXT | Source entity URI. |
| `predicate` | TEXT | Relationship: `forwarded_from`, `linked_group`, `mentions`, `replied_to`, `member_of`, `admin_of`, `commented_on`, `recommended_with`, `invited_via`, `gifted_to`. |
| `object_uri` | TEXT | Target entity URI. |
| `observed_at` | TEXT | When the edge was observed. |
| `tier` | TEXT | Visibility tier. |
| `source_raw_id` | INTEGER | Provenance. |
| `evidence_json` | TEXT | Optional supporting detail (e.g. the message the edge came from). |

## Sync & operations

### `sync_state` — resume cursors

| Column | Type | Meaning |
|---|---|---|
| `scope` | TEXT | Namespace (e.g. `channel`, `history`). |
| `key` | TEXT | Key within scope (usually a channel id). |
| `value_json` | TEXT | Cursor value — notably the channel `pts` for incremental sync, and phase offsets. |

Primary key `(scope, key)`.

### `sync_ranges` — verified-complete message-id ranges

Contiguous `[lo, hi]` id ranges known to be fully fetched for a channel. Gaps
between ranges are candidates for deletion probing.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `channel_id` | INTEGER | Channel. |
| `lo` / `hi` | INTEGER | Inclusive id bounds of a verified-complete run. |

### `flood_log` — rate-limit cooldowns

Persisted `FLOOD_WAIT` cooldowns so a restart doesn't immediately re-trip the
same wall.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `method` | TEXT | The RPC method that was flooded. |
| `until` | TEXT | Cooldown expiry (UTC). |
| `seconds` | INTEGER | The wait Telegram asked for. |
| `recorded_at` | TEXT | When recorded. |

### `run_events` — per-phase bookkeeping

One row per collector phase per run: completion, skip, phase-stop, hard-stop,
with detail — the audit trail of what happened during a `collect`.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `observed_at` | TEXT | When. |
| `channel_id` | INTEGER | Channel (NULL if the channel phase itself stopped early). |
| `phase` | TEXT | Collector name (`channel`, `history`, …). |
| `kind` | TEXT | `complete`, `skip`, `phase_stop`, `hard_stop`. |
| `detail_json` | TEXT | Counts, or the error that stopped the phase. |

### `custody_log` — chain of custody

SHA-256 of every file paperboy writes to disk, for forensic integrity.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `path` | TEXT | File path. |
| `sha256` | TEXT | Hash at write time. |
| `recorded_at` | TEXT | When. |
| `source_message_uri` | TEXT | Message the file came from, if any. |

## Full-text search

### `messages_fts` — FTS5 index over message text

An external-content FTS5 virtual table over `messages.text`, kept in sync by
triggers on insert/update/delete. Datasette auto-detects it and wires a search
box onto `messages`. Query it directly:

```sql
SELECT m.msg_id, m.text
FROM messages_fts f JOIN messages m ON m.rowid = f.rowid
WHERE messages_fts MATCH 'your query';
```

The `messages_fts_data` / `messages_fts_idx` / `messages_fts_docsize` /
`messages_fts_config` tables are FTS5's internal shadow tables — ignore them.

## Meta

### `schema_migrations`

Tracks which migration files have been applied (`name` per applied migration),
so `Store.open` only runs new ones. Internal; not for querying.
