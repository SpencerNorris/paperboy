-- 0002_web: `web_snapshots` — the `web` collector's `t.me/s/<name>` post
-- captures (source='tme') and Wayback CDX index rows (source='wayback').
--
-- Distinct filename from 0001_init (spec: migrations apply by unique stem,
-- so this coexists with any other Phase-2 feature's own 0002_*.sql).

CREATE TABLE IF NOT EXISTS web_snapshots (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL CHECK (source IN ('tme', 'wayback')),
    url               TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    channel_username  TEXT NOT NULL,
    msg_id            INTEGER,
    timestamp         TEXT,
    content_hash      TEXT,
    raw               TEXT,
    meta_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_snapshots_channel
    ON web_snapshots(channel_username, msg_id);
CREATE INDEX IF NOT EXISTS idx_web_snapshots_source
    ON web_snapshots(source, fetched_at);
