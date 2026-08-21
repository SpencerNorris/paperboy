-- 0001_init: raw log, entity/history/edge projections, sync state, search.
--
-- Every CREATE is IF NOT EXISTS: migrations run via sqlite3.executescript,
-- which auto-commits each DDL statement as it runs (no multi-statement
-- transaction wraps the whole file), so a migration interrupted partway
-- through must be safe to re-run from the top. See store/db.py.

-- ── Raw (system of record) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_records (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    tier         TEXT NOT NULL,
    context_json TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_records_kind ON raw_records(kind, observed_at);

-- ── Entities (current state) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY,
    uri                 TEXT NOT NULL UNIQUE,
    username            TEXT,
    title               TEXT,
    about               TEXT,
    kind                TEXT,
    created_at          TEXT,
    linked_chat_id      INTEGER,
    participants_count  INTEGER,
    flags_json          TEXT,
    restriction_json    TEXT,
    source_raw_id       INTEGER REFERENCES raw_records(id),
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peers (
    uri            TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    id             INTEGER NOT NULL,
    access_hash    INTEGER,
    is_min         INTEGER NOT NULL DEFAULT 0,
    seen_in_chat   INTEGER,
    seen_in_msg    INTEGER,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    flags_json     TEXT,
    source_raw_id  INTEGER REFERENCES raw_records(id),
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    uri              TEXT PRIMARY KEY,
    channel_id       INTEGER NOT NULL,
    msg_id           INTEGER NOT NULL,
    date             TEXT,
    edit_date        TEXT,
    from_uri         TEXT,
    post_author      TEXT,
    text             TEXT NOT NULL DEFAULT '',
    entities_json    TEXT,
    media_kind       TEXT,
    media_json       TEXT,
    fwd_json         TEXT,
    reply_to_msg_id  INTEGER,
    reply_to_top_id  INTEGER,
    grouped_id       INTEGER,
    via_bot_id       INTEGER,
    is_service       INTEGER NOT NULL DEFAULT 0,
    action_json      TEXT,
    content_hash     TEXT NOT NULL,
    deleted_at       TEXT,
    source_raw_id    INTEGER REFERENCES raw_records(id),
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_channel_msg ON messages(channel_id, msg_id);

CREATE TABLE IF NOT EXISTS media (
    sha256          TEXT PRIMARY KEY,
    message_uri     TEXT REFERENCES messages(uri),
    kind            TEXT,
    mime_type       TEXT,
    size            INTEGER,
    file_name       TEXT,
    attributes_json TEXT,
    path            TEXT,
    downloaded_at   TEXT,
    exif_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_message ON media(message_uri);

-- ── History (append-only) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_snapshots (
    id                  INTEGER PRIMARY KEY,
    channel_id          INTEGER NOT NULL,
    observed_at         TEXT NOT NULL,
    participants_count  INTEGER,
    online_count        INTEGER,
    title               TEXT,
    username            TEXT,
    about_hash          TEXT,
    source_raw_id       INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_channel_snapshots_channel ON channel_snapshots(channel_id, observed_at);

CREATE TABLE IF NOT EXISTS message_revisions (
    id             INTEGER PRIMARY KEY,
    message_uri    TEXT NOT NULL REFERENCES messages(uri),
    observed_at    TEXT NOT NULL,
    edit_date      TEXT,
    content_hash   TEXT NOT NULL,
    text           TEXT,
    entities_json  TEXT,
    media_json     TEXT,
    source_raw_id  INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_message_revisions_msg ON message_revisions(message_uri, observed_at);

CREATE TABLE IF NOT EXISTS message_metrics (
    id             INTEGER PRIMARY KEY,
    message_uri    TEXT NOT NULL REFERENCES messages(uri),
    observed_at    TEXT NOT NULL,
    views          INTEGER,
    forwards       INTEGER,
    replies        INTEGER,
    reactions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_metrics_msg ON message_metrics(message_uri, observed_at);

-- No FK to messages(uri): a `gap`/`empty` tombstone can be recorded for a
-- message id that was probed but never itself observed/stored (spec §7 —
-- deletion evidence for ids that never appeared in a verified-complete
-- range or in any page of history).
CREATE TABLE IF NOT EXISTS message_tombstones (
    id           INTEGER PRIMARY KEY,
    message_uri  TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    evidence     TEXT NOT NULL CHECK (evidence IN ('update', 'gap', 'empty'))
);
CREATE INDEX IF NOT EXISTS idx_message_tombstones_msg ON message_tombstones(message_uri, observed_at);

-- ── Edges (triple-shaped graph) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    id             INTEGER PRIMARY KEY,
    subject_uri    TEXT NOT NULL,
    predicate      TEXT NOT NULL,
    object_uri     TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    tier           TEXT NOT NULL,
    source_raw_id  INTEGER REFERENCES raw_records(id),
    evidence_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_subject ON edges(subject_uri, predicate, object_uri);

-- ── Sync ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_state (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS sync_ranges (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    lo          INTEGER NOT NULL,
    hi          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_ranges_channel ON sync_ranges(channel_id, lo);

CREATE TABLE IF NOT EXISTS flood_log (
    id           INTEGER PRIMARY KEY,
    method       TEXT NOT NULL,
    until        TEXT NOT NULL,
    seconds      INTEGER NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flood_log_method ON flood_log(method, recorded_at);

-- ── Run bookkeeping ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_events (
    id           INTEGER PRIMARY KEY,
    observed_at  TEXT NOT NULL,
    channel_id   INTEGER,
    phase        TEXT,
    kind         TEXT NOT NULL,
    detail_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_events_channel ON run_events(channel_id, observed_at);

-- ── Custody ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS custody_log (
    id                  INTEGER PRIMARY KEY,
    path                TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    source_message_uri  TEXT
);

-- ── Full-text search (Datasette-friendly external-content FTS5) ─────────
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
