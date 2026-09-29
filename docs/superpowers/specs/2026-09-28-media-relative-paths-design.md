# Media locations as profile-relative keys (#62)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #62.
**Order:** 2 of 5 in the sequential chain (after #69; read
`2026-09-28-media-storage-overview.md` first, including the live smoke
protocol).
**ADR:** lands `docs/adr/0007-media-keys.md` before any code (storage is a
settled decision; this amends how ADR-0002's `media`/`custody_log` locate
files).

## 1. Problem

`media.path`, `custody_log.path` and the `MediaDownload`/`AvatarDownload` raw
payloads store `str(path)` exactly as the run built it from
`settings.data_dir`. The live store therefore mixes two forms (26 Sep 2026
counts):

| table | repo-relative `data/default/media/…` | absolute `/…/data/default/media/…` |
|---|---|---|
| `custody_log` | 933 | 211 |
| `media` | 609 | 146 |

Relative forms only resolve from the launching cwd; absolute forms pin the
store to one machine's mount. Both block moving the store to the VM and the
bytes to GCS (#63). A downstream catalogue already had to stop trusting
stored paths.

## 2. Decision (ADR-0007)

A media location is a **key**: a POSIX-style string relative to the profile
directory, always `media/<sha256[:2]>/<sha256><ext>`. It never contains the
data dir, the profile name, a drive or a leading `/`. It is resolved against
the configured storage root at read time.

- New module `src/paperboy/media_keys.py`:
  - `media_key(sha256: str, ext: str) -> str` — the only constructor.
    Validates `sha256` is 64 lowercase hex and `ext` is `""` or `.` + a short
    extension with no `/`.
  - `resolve_media_key(settings, profile, key) -> Path` —
    `profile_dir(settings, profile) / key`, rejecting keys that are absolute
    or contain `..` (a stored key is data, never trusted as a path).
  - `normalize_legacy_location(value: str, sha256: str) -> str` — maps either
    legacy form to a key using the sha, not string surgery on directories:
    the filename is the substring of `value` starting at `sha256`
    (`<sha><ext>`), so the key is `media/<sha[:2]>/` + that. Raises on a value
    that doesn't contain its own sha (log and fail loudly — never guess).
- `#63` will later add a GCS root; the key format is the object name suffix
  under a prefix, unchanged. Design nothing GCS-specific here beyond keeping
  keys backend-neutral.

## 3. Changes

1. **Writers** — `collectors/media.py` and `collectors/profiles.py`
   (`_download_avatar`): compute `key = media_key(sha, ext)`, write the bytes
   to `resolve_media_key(...)`, store `key` in `media.path`,
   `custody_log.path` and the raw payload's `"path"`. Leave the byte-writing
   mechanics as they are — #64 (next in the chain) replaces them and will
   call `media_key`/`resolve_media_key` directly.
2. **Migration `0005_media_keys.sql`** — rewrite existing rows in place with
   pure SQL, using the sha to anchor (works for both legacy forms):
   `path = 'media/' || substr(sha256,1,2) || '/' || substr(path, instr(path, sha256))`
   for rows where `instr(path, sha256) > 0` and `path NOT LIKE 'media/%'`.
   Rows where the sha isn't in the path must be left untouched **and**
   counted: add a check (test + a startup log line from `Store.open` if any
   such rows remain) — no silent partial migration. Applies to `media` and
   `custody_log`.
3. **Raw stays append-only** — old `MediaDownload`/`AvatarDownload` payloads
   keep their legacy `path`. `replay.py`'s `download_media` /
   `download_user_photo` stop trusting `payload["path"]` as a filesystem path:
   derive the key via `normalize_legacy_location(payload["path"], sha)` (a
   key-form payload passes through unchanged) and resolve it under the
   source's media root. This replaces today's "try the stored path, then
   re-derive" fallback with one rule.
4. **Readers** — grep for every read of `media.path` / `custody_log.path`
   (today: `replay.py`, `_load_content_index`/`_lookup_by_sha` in
   `media.py`; the content index returns the stored value, which becomes a
   key — callers that need bytes resolve it). Update
   `docs/data-model.md` (`media`, `custody_log` sections) to say "key,
   relative to the profile dir".

## 4. Tests (write first, see them fail)

- `media_key`/`resolve_media_key`/`normalize_legacy_location` unit tests,
  including: both legacy forms → same key; a path lacking its sha → raises;
  `../` and absolute keys rejected by `resolve_media_key`.
- Migration: a store seeded with both legacy forms (use the counts' shapes)
  migrates to keys; a row whose path lacks its sha is untouched and reported.
- Run from a non-repo cwd (`monkeypatch.chdir(tmp_path)`) with a relative
  `data_dir` and with an absolute one → identical stored keys.
- Store moved to a new root: copy profile dir elsewhere, point `data_dir` at
  it, `reproject` succeeds and resolves every media file.
- `reproject` parity: an archive whose raw payloads hold legacy absolute
  paths reprojects to key-form rows (update the golden files deliberately,
  with the diff reviewed).

## 5. Definition of done (smoke on real data)

Offline, on a **backup copy** of the live store in the scratch data dir (per
the overview's live smoke protocol §1 — `sqlite3 … ".backup …"`, never
migrate the original):

```
sqlite3 copy.sqlite "select count(*) from media where path not like 'media/%'"   # before: ~755 (26 Sep count)
uv run python -c 'from paperboy.store.db import Store; Store.open("copy.sqlite")' # applies 0005
sqlite3 copy.sqlite "select count(*) from media where path not like 'media/%'"   # after: 0
sqlite3 copy.sqlite "select count(*) from custody_log where path not like 'media/%'"  # after: 0
```
plus: every migrated key resolves to an existing file under
`data/default/` (script: count missing = 0), and `paperboy reproject --out
scratch.sqlite` on the original store succeeds with key-form rows. Paste the
real output.

## 6. Out of scope

GCS (#63), streaming (#64), EXIF (#16). Rewriting `raw_records`.
