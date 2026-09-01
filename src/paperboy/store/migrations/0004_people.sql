-- 0004_people: the person layer (spec §4). Profile richness lives HERE, never
-- in `peers` (which stays the min-provenance stub table). Every CREATE is IF
-- NOT EXISTS for the same re-runnable-migration reason as 0001_init.

-- current profile state — one row per user (spec §4 `users`)
CREATE TABLE IF NOT EXISTS users (
    uri               TEXT PRIMARY KEY,
    id                INTEGER NOT NULL,
    access_hash       INTEGER,
    tier              TEXT NOT NULL,
    is_min            INTEGER NOT NULL DEFAULT 0,
    username          TEXT,
    usernames_json    TEXT,
    first_name        TEXT,
    last_name         TEXT,
    phone             TEXT,
    about             TEXT,
    birthday          TEXT,
    emoji_status      TEXT,
    color_json        TEXT,
    status_kind       TEXT,
    status_value      TEXT,
    photo_ref         TEXT,
    restriction_json  TEXT,
    bot_json          TEXT,
    flags_json        TEXT,
    field_states_json TEXT,
    enriched_at       TEXT,
    source_raw_id     INTEGER REFERENCES raw_records(id),
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_enriched ON users(enriched_at);

-- append-only observation log — one row per (user, method) observation whose
-- bundle hash changed (spec §4 `user_snapshots`)
CREATE TABLE IF NOT EXISTS user_snapshots (
    id             INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    tier           TEXT NOT NULL,
    method         TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    fields_json    TEXT NOT NULL,
    source_raw_id  INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_user_snapshots_uri ON user_snapshots(uri, observed_at);

-- dated avatar history (photos.getUserPhotos) + the downloaded file, if any
CREATE TABLE IF NOT EXISTS user_photos (
    id             INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    photo_id       INTEGER NOT NULL,
    date           TEXT,
    dc_id          INTEGER,
    has_video      INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT REFERENCES media(sha256),
    observed_at    TEXT NOT NULL,
    source_raw_id  INTEGER REFERENCES raw_records(id),
    UNIQUE (uri, photo_id)
);

-- roster membership facts, keyed (group, user) (spec §4 `participants`).
-- `left` covers both channelParticipantLeft and the oracle's
-- USER_NOT_PARTICIPANT — Telegram does not distinguish "left" from "never
-- joined" for a non-admin. `join_date` is stored only where `date` MEANS
-- join (member/admin/self); a Banned `date` is the ban date and stays in raw.
CREATE TABLE IF NOT EXISTS participants (
    group_id                 INTEGER NOT NULL,
    uri                      TEXT NOT NULL,
    status                   TEXT NOT NULL
        CHECK (status IN ('member', 'admin', 'creator', 'banned', 'left')),
    join_date                TEXT,
    rank                     TEXT,
    subscription_until_date  TEXT,
    inviter_id               INTEGER,
    source_raw_id            INTEGER REFERENCES raw_records(id),
    first_seen               TEXT NOT NULL,
    last_seen                TEXT NOT NULL,
    PRIMARY KEY (group_id, uri)
);
CREATE INDEX IF NOT EXISTS idx_participants_uri ON participants(uri);

-- append-only membership observations: one row per enumerated member per
-- run (uri set) PLUS one roster-level accounting row per (group, run) with
-- uri NULL carrying `enumerated / true_count` and, when walled, `reason`.
CREATE TABLE IF NOT EXISTS participant_snapshots (
    id                       INTEGER PRIMARY KEY,
    group_id                 INTEGER NOT NULL,
    observed_at              TEXT NOT NULL,
    uri                      TEXT,
    status                   TEXT,
    join_date                TEXT,
    rank                     TEXT,
    subscription_until_date  TEXT,
    enumerated               INTEGER,
    true_count               INTEGER,
    reason                   TEXT,
    source_raw_id            INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_participant_snapshots_group
    ON participant_snapshots(group_id, observed_at);

-- `profiles` scheduling state: the newest `users.getFullUser` ATTEMPT per
-- user, whatever its outcome (plan D3 as amended after the Leg 2 review).
-- `users.enriched_at` moves only when full columns were actually applied, so
-- it cannot double as the rotation key — a user whose fetch permanently fails
-- would sit at the head of every run and starve the rest. Written at the one
-- point where a budget slot is spent (`outcome='attempted'`, before the RPC
-- answers) and replaced by the arm that finishes the attempt; read by
-- `ProfilesCollector._enrichment_candidates`. Bookkeeping, not a projection —
-- excluded from round-trip identity like `sync_state`.
CREATE TABLE IF NOT EXISTS profile_attempts (
    uri           TEXT PRIMARY KEY,
    attempted_at  TEXT NOT NULL,
    outcome       TEXT NOT NULL
        CHECK (outcome IN ('attempted', 'enriched', 'skipped', 'malformed', 'not_projected')),
    detail        TEXT
);
