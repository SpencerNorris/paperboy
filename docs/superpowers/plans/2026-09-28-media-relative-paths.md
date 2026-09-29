# Implementation plan — #62 media locations as profile-relative keys

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-28-media-relative-paths.md` in your first commit.)

Branch: cut `fix/media-relative-paths` from `origin/dev/media-storage` (05746b0). Per task: `uv run pytest -q` (with `TMPDIR` on a volume with free space), `uv run ruff check`, `uv run pyright` green before committing. Commits end with `Co-Authored-By:` only. Never commit a local machine path, channel name/id or message text (public repo).

Spec: `docs/superpowers/specs/2026-09-28-media-relative-paths-design.md` (read the overview spec first). Code touched: new `src/paperboy/media_keys.py`; `collectors/media.py`, `collectors/profiles.py`, `replay.py`, `app.py`, `store/db.py`, `store/migrations/0006_media_keys.sql`; tests listed per task; `docs/adr/0007-media-keys.md`, `docs/data-model.md`, `docs/features/reproject.md`, `CLAUDE.md`.

Facts checked read-only against the live store on 2026-09-29 (never migrate it): `media` 755 rows, all legacy (146 absolute, 609 `data/…`-relative); `custody_log` 1144, all legacy; **0 rows in either table lack their own sha in `path`**; no NULL paths; 755 legacy `MediaDownload` raw payloads, no `AvatarDownload` rows; extensions in use: `.jpg .mp4 .MP4 .MOV .pdf .mkv` — **uppercase extensions exist**.

## Ambiguities / contradictions and recommended resolutions

1. **Migration number.** Spec §3.2 says `0005_media_keys.sql`, but `0005_flood_applied.sql` already exists on the base (#69). Resolution: `src/paperboy/store/migrations/0006_media_keys.sql`; `Store._apply_migrations` (`store/db.py:78`) globs in sorted order, nothing else to change.
2. **Migration WHERE guard.** Spec's `path NOT LIKE 'media/%'` would skip a legacy row whose data dir happens to be named `media`. Resolution: rewrite every row whose value differs from its canonical key — `WHERE instr(path, sha256) > 0 AND path <> 'media/' || substr(sha256,1,2) || '/' || substr(path, instr(path, sha256))` — idempotent, and the same predicate (plus `path IS NULL OR instr(path, sha256) = 0`) is the "still unnormalised" check. The DoD's simpler `NOT LIKE 'media/%'` counts remain valid for the smoke.
3. **`ReplaySource.media_root` semantics** (`replay.py:69-86`, `app.py:162`). A key is profile-relative (`media/…`), so replay must resolve it against the *profile dir*, not `<profile>/media`. Resolution: rename the constructor arg/attribute to `profile_root` and pass `profile_dir(settings, profile)` from `app.py:162`. Tests that pass `tmp_path / "media"` (`test_replay_gateway.py:75,184,193,211-435`, `test_replay_people.py:106,177`, `test_replay_web.py:30`) become `tmp_path`; `tmp_path / "default" / "media"` (`test_reproject.py:32,49,449`, `test_reproject_people.py:265`) becomes `tmp_path / "default"`. The seeded files (`test_replay_gateway.py:61`, `test_replay_people.py:81`) already sit under `<root>/media/<xx>/`, so they still resolve.
4. **Extension validation vs real data.** Spec: ext is `""` or `.` + "a short extension with no `/`". Real keys carry `.MP4`/`.MOV`; Linux/GCS are case-sensitive. Resolution: `media_key` **preserves case and never folds it**; valid ext = `re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}", ext)` with no `..`. `_guess_ext` (`media.py:99-114`) returns `""` (with a WARNING) when a filename's suffix fails that rule, so a hostile `DocumentAttributeFilename` can never make `media_key` raise mid-phase. The SQL migration does not validate ext (the on-disk name is whatever was written).
5. **Legacy normaliser failure inside replay.** Spec: `normalize_legacy_location` raises when the sha is absent. Inside `RawReplayGateway.download_media`/`download_user_photo` a raise would abort the whole reproject for one bad payload. Resolution: catch `ValueError` there and raise `SkipAndRecord("replay: unusable media location for sha …")` — same class as today's "media file missing" skip (`replay.py:532`), logged by the collector. Everywhere else (writers, tests) it stays a hard `ValueError`.
6. **Where the startup check lives.** `Store` has no logger. Resolution: `log = logging.getLogger("paperboy.store")` in `store/db.py`; after `_apply_migrations()` in `Store.open` (`db.py:69`) run `Store.unnormalised_media_counts() -> dict[str, int]` (`media`, `custody_log`) and log one WARNING only when any count > 0 (counts only — never a path, so nothing for `RedactionFilter` to scrub). Cost: two `count(*)` over ≤ ~1k rows per open.
7. **Readers in `media.py`** (`_load_content_index` 294-314, `_lookup_by_sha` 316-318, `_record_custody` 320-327). They return the stored value verbatim; after 0006 that is a key. Do **not** add normalising/repair logic there: both writers embed the sha (`media.py:259`, `profiles.py:648`), so a row lacking it cannot come from paperboy (live store: 0), and the `Store.open` warning covers tampered stores. Rename locals/docstrings `path` → `key` only.
8. **Parity golden.** Spec §4's "archive whose raw payloads hold legacy absolute paths" is *not* what `parity_golden.json` exercises (it is regenerated from a fresh collect, key-form). Resolution: regenerate the golden (Task 5) and prove the legacy-payload case with a dedicated test (Task 5) — the golden diff must touch only `path` values.
9. **API surface for #64/#70/#68.** Keep it to: `media_key`, `is_media_key`, `resolve_key_under(root, key)`, `resolve_media_key(settings, profile, key)`, `media_dir(settings, profile)`, `normalize_legacy_location`. #64 needs `media_dir` (disk floor, `.incoming/`) and the two resolvers; #70 copies by key via `resolve_key_under`; #68 prints `media.path` as the key. No GCS-specific code.

## Task 0 — ADR-0007 (before any code) + this plan file

`docs/adr/0007-media-keys.md` per `~/.claude/rules/adr-format.md`: Status `Accepted (2026-09-29, #62)`; Context (the mixed-forms table from spec §1; blocks the VM move and #63; a downstream catalogue stopped trusting stored paths); Options considered (a: always absolute; b: cwd-relative; c: profile-relative key resolved at read time — chosen; d: store no location and derive from `sha256` — rejected: ext is not a column and `custody_log` records a location per write); Decision (key format `media/<sha[:2]>/<sha><ext>`, POSIX separators, ext case preserved, the single constructor, resolution against `profile_dir`, legacy normalisation anchored on the sha, migration 0006 rewrites `media`/`custody_log` in place, `raw_records` untouched, replay normalises old payloads, unnormalisable rows are counted and warned — never guessed); Consequences (store is portable across cwd/machine/backend; #63 prefixes the key; `status`/`export` opening an old store apply 0006 like every prior migration; a stored key is data — always validated before use as a path); Notes (spec, plan, #62, ADR-0002 §"Datasette-friendly … custody_log"). Append one line to ADR-0002's Decision: "Media locations are keys — see ADR-0007."
Commit: `docs(adr): ADR-0007 media locations are profile-relative keys (#62)` (includes the plan file).

## Task 1 — `media_keys.py`

Tests first, new `tests/test_media_keys.py` (`SHA = "ab" + "0"*62`):
- `test_media_key_builds_profile_relative_key`: `media_key(SHA, ".pdf") == f"media/ab/{SHA}.pdf"`; `media_key(SHA, "") == f"media/ab/{SHA}"`.
- `test_media_key_preserves_extension_case`: `.MP4` stays `.MP4`.
- `test_media_key_rejects_bad_sha`: uppercase hex, 63 chars, non-hex → `ValueError`.
- `test_media_key_rejects_bad_ext`: `"pdf"`, `"."`, `".."`, `".a/b"`, `".a\\b"`, `". x"`, `"." + "a"*17` → `ValueError`.
- `test_is_media_key`: true for a canonical key; false for `data/default/media/ab/<sha>.jpg`, `/abs/…`, `media/ab/<sha>/x`.
- `test_resolve_media_key_joins_profile_dir`: `load_settings("default", {"data_dir": tmp_path})`, profile `"p"` → `tmp_path / "p" / "media" / "ab" / f"{SHA}.pdf"`.
- `test_resolve_rejects_non_keys`: `"/etc/passwd"`, `"media/../x"`, `f"media/ab/../../{SHA}"`, `""`, `"C:\\x"`, `f"data/default/media/ab/{SHA}.jpg"` → `ValueError` (strict: only `is_media_key` values resolve).
- `test_normalize_legacy_both_forms_give_same_key`: `f"data/default/media/ab/{SHA}.jpg"`, `f"/mnt/x/data/default/media/ab/{SHA}.jpg"`, `f"C:\\d\\media\\ab\\{SHA}.jpg"` and the key itself all → `f"media/ab/{SHA}.jpg"`.
- `test_normalize_legacy_without_sha_raises`: `"elsewhere/nothing.bin"` → `ValueError`; ext after the sha that fails rule 4 → `ValueError`.
- `test_media_dir`: `== tmp_path / "p" / "media"`.
Code: `src/paperboy/media_keys.py` — module docstring citing ADR-0007; `MEDIA_PREFIX = "media"`; `_SHA_RE`, `_EXT_RE`, `_KEY_RE`; the six functions from ambiguity 9. `normalize_legacy_location(value, sha)`: `i = value.find(sha)`; `< 0` → `ValueError`; `return media_key(sha, value[i + 64:])` (no directory string surgery). Import `profile_dir` from `paperboy.config` (no cycle: `config` imports nothing from `store`/collectors).
Commit: `feat(media): media_keys — profile-relative media keys and legacy normalisation (#62)`

## Task 2 — migration 0006 + `Store.open` check

Tests first (`tests/test_store_migrations.py`), helper `_unapply(db_path, name)` = open, `DELETE FROM schema_migrations WHERE name=?`, close (this is exactly the state of a pre-#62 store):
- `test_0006_media_keys_rewrites_both_legacy_forms`: open a store; insert `media` rows for sha A (`data/default/media/aa/<A>.jpg`), sha B (`/mnt/x/data/default/media/bb/<B>.MP4`), sha C (already a key), and `custody_log` rows for A twice (both forms), B, C; `_unapply(…, "0006_media_keys")`; reopen; assert every path equals its canonical key, `.MP4` case preserved, C unchanged, `"0006_media_keys"` in `schema_migrations`.
- `test_0006_leaves_rows_without_sha_untouched_and_warns(caplog)`: row `path='elsewhere/nothing.bin'` in `media` → unchanged after reopen; a `paperboy.store` WARNING containing `media=1` and `custody_log=0`; a clean store emits no such record.
- `test_0006_is_idempotent`: `_unapply` + reopen twice → identical rows.
Code: `0006_media_keys.sql` (header comment: why, the anchor rule, "rows lacking their sha are left and reported by Store.open"); two `UPDATE`s with the predicate from ambiguity 2. `store/db.py`: logger, `unnormalised_media_counts()`, the WARNING in `open` after migrations.
Commit: `feat(store): migration 0006 normalises media/custody_log paths to keys; warn on leftovers (#62)`

## Task 3 — writers store keys

Tests first:
- `tests/test_collector_media.py:88-108`: `key = f"media/{sha[:2]}/{sha}.pdf"`; file at `tmp_path / "p" / key`; `row["path"] == key`; `custody[0]["path"] == key`; add: the `MediaDownload` payload's `"path"` == key (`json.loads(payload_json)`).
- new `test_key_is_identical_from_any_cwd_and_data_dir_form(tmp_path, monkeypatch)`: `monkeypatch.chdir(tmp_path)`; settings A `{"data_dir": Path("data")}` (relative), settings B `{"data_dir": tmp_path / "data2"}` (absolute); collect the same doc into two stores; both `media.path == key`; files exist at `tmp_path/"data"/"p"/key` and `tmp_path/"data2"/"p"/key`.
- new `test_guess_ext_drops_unusable_suffix`: file_name `"report.pdf "` → `""`; `"clip.MP4"` → `".MP4"`.
- `tests/test_collector_profiles.py:439-440`: `assert {m["path"] for m in media} == {f"media/{sha[:2]}/{sha}.jpg"}` with `sha = sha256(photo_bytes)`.
Code: `media.py` — drop `media_root` (136); at 258-266 `ext = _guess_ext(...)`, `key = media_key(sha, ext)`, `path = resolve_media_key(ctx.settings, ctx.profile, key)`, keep the write-if-missing block, replace `path_str` with `key` at 270/279/282/290; update the module docstring (1-6) and `_load_content_index`/`_lookup_by_sha` docstrings ("key, relative to the profile dir"). `profiles.py` — remove the `media_root` parameter (605, 619, 627); 648: `key = media_key(sha, ".jpg")`, `path = resolve_media_key(...)`; store `key` at 653/662/667. Byte-writing mechanics unchanged (#64 replaces them).
Commit: `fix(media,profiles): store profile-relative media keys, not run-dependent paths (#62)`

## Task 4 — replay resolves keys, never trusts `payload["path"]`

Tests first: apply the mechanical renames from ambiguity 3 (`sed` over the listed files; `_seed` return values keep their names). Then:
- `tests/test_replay_gateway.py`: `test_download_media_normalises_legacy_absolute_payload_path` — seed a payload whose `path` is `str(tmp_path / "gone" / "data" / "default" / "media" / xx / f"{sha}.txt")` (dir never created) while the file sits at `tmp_path / "media" / xx / …` → bytes returned. `test_download_media_payload_without_sha_is_a_skip` — payload `path="bogus.bin"` → `SkipAndRecord`, not `ValueError`.
- `tests/test_replay_people.py`: same two for `download_user_photo` (line 86 already seeds a foreign path — keep it; it now proves normalisation).
- `test_full_user_photos_and_avatar_bytes` (148) and `test_download_media_reads_content_addressed_file` (160) unchanged.
Code: `replay.py:66-86` (`profile_root`, docstring), `download_media` 521-533 and `download_user_photo` 668-673 → `try: key = normalize_legacy_location(payload["path"], sha) except ValueError as exc: raise SkipAndRecord(...) from exc`; `path = resolve_key_under(self._src.profile_root, key)`; missing file → the existing `SkipAndRecord`. Delete the "try stored path, then re-derive" comment/branch. `app.py:162-163` → `ReplaySource.open(source_db, profile_dir(settings, profile))`.
Commit: `fix(replay): resolve media by key under the source profile dir; normalise legacy payload paths (#62)`

## Task 5 — moved-store test, parity golden, docs

Tests first (`tests/test_reproject.py`), `test_reproject_from_moved_profile_dir_with_legacy_payloads(tmp_path, monkeypatch)`: `run_full_collect(tmp_path)`; simulate a pre-#62 archive by `UPDATE raw_records SET payload_json = json_set(payload_json, '$.path', <absolute legacy path under tmp_path>) WHERE lower(kind) IN ('mediadownload','avatardownload')` on the source (test setup only); `shutil.copytree(tmp_path/"default", new_root/"default")`; `shutil.rmtree(tmp_path/"default")` (old root gone); `PAPERBOY_DATA_DIR=new_root`; CLI `reproject --out new_root/out.sqlite` exits 0; `media` count equals the source's; every `media.path`/`custody_log.path` satisfies `is_media_key`; every key resolves to an existing file under `new_root/"default"`.
Golden: `UPDATE_GOLDEN=1 uv run pytest tests/test_reproject_parity.py -q`, then `git diff tests/fixtures/reproject/parity_golden.json | grep -E '^[-+] ' | grep -v '"path"'` must print nothing except `payload_json` lines for `MediaDownload` (whose only change is `path`) — paste the reviewed diff summary in the PR. `dump_db` (`test_reproject_parity.py:160-205`) needs no change.
Docs: `docs/data-model.md:136` and `:280` → "Media key, relative to the profile dir: `media/<sha[:2]>/<sha><ext>` — resolve with `media_keys.resolve_media_key` (ADR-0007)"; `docs/features/reproject.md:127-131` → replay resolves each payload's location as a key under the source profile dir (legacy absolute/relative payloads normalised by sha); `CLAUDE.md` "Settled decisions" → one bullet: media locations are profile-relative keys (ADR-0007), never absolute or cwd-relative paths.
Commit: `test(reproject): moved-profile reproject with legacy payloads; regenerate parity golden; docs (#62)`

## Task 6 — Definition of done (smoke on real data, offline) + PR

`S` = the scratch data dir named in the handoff prompt (outside the repo, on the data volume); `<real>` = the real data dir. Paste real output for every line.
```
mkdir -p $S/default && sqlite3 <real>/default/paperboy.sqlite ".backup '$S/default/paperboy.sqlite'"
ln -s <real>/default/media $S/default/media          # offline symlink per overview §1
find -L $S/default/media -type f | wc -l; du -sk -L $S/default/media   # BEFORE (count, size)
sqlite3 $S/default/paperboy.sqlite "select count(*) from media where path not like 'media/%'; select count(*) from custody_log where path not like 'media/%'"   # expect 755 / 1144
sqlite3 $S/default/paperboy.sqlite "select count(*) from raw_records where lower(kind) in ('mediadownload','avatardownload') and json_extract(payload_json,'$.path') not like 'media/%'"   # 755 (legacy raw present)
PAPERBOY_DATA_DIR=$S uv run python -c "from pathlib import Path; from paperboy.store.db import Store; Store.open(Path('$S/default/paperboy.sqlite')).close()"   # applies 0006; must log NO leftover warning
sqlite3 $S/default/paperboy.sqlite "select count(*) from media where path not like 'media/%'; select count(*) from custody_log where path not like 'media/%'; select name from schema_migrations where name like '0006%'"   # 0 / 0 / 0006_media_keys
sqlite3 $S/default/paperboy.sqlite "select count(*) from raw_records where lower(kind) in ('mediadownload','avatardownload') and json_extract(payload_json,'$.path') not like 'media/%'"   # still 755 — raw untouched
PAPERBOY_DATA_DIR=$S uv run python - <<'EOF'      # every migrated key resolves; print missing per table
import sqlite3, os; from pathlib import Path
from paperboy.media_keys import resolve_key_under, is_media_key
root = Path(os.environ["PAPERBOY_DATA_DIR"]) / "default"; c = sqlite3.connect(root / "paperboy.sqlite")
for t in ("media", "custody_log"):
    rows = c.execute(f"select path from {t}").fetchall()
    bad = [p for (p,) in rows if not is_media_key(p)]; missing = [p for (p,) in rows if is_media_key(p) and not resolve_key_under(root, p).exists()]
    print(t, "rows", len(rows), "non-key", len(bad), "missing-file", len(missing))
EOF
PAPERBOY_DATA_DIR=$S uv run paperboy reproject --profile default --out $S/reprojected.sqlite
sqlite3 $S/reprojected.sqlite "select count(*) from media; select count(*) from media where path not like 'media/%'; select count(*) from custody_log where path not like 'media/%'"   # N / 0 / 0
find -L $S/default/media -type f | wc -l; du -sk -L $S/default/media   # AFTER — must equal BEFORE (reproject never writes media)
uv run pytest -q && uv run ruff check && uv run pyright
```
Expected `missing-file`: `docs/features/reproject.md` records two files genuinely absent from this profile's media dir, so `media missing-file` may be 2 (and `custody_log` the matching rows) — report the number and reconcile it with reproject's `unavailable` count; anything else is a finding. The reproject writes only to `$S`; its log goes to `$S/default/paperboy.log`.

Live smoke: NONE for #62 (orchestrator decision: the spec DoD is offline-only, and the operator authorized only the live smokes a spec DoD names). Make NO live Telegram calls in this feature; the offline smokes above are the DoD.

PR targets `dev/media-storage`; body = DoD transcript (redacted), golden-diff summary, ambiguity decisions, closes #62.

## Edge cases and failure modes (assert or document each)

- Uppercase extensions (`.MP4`, `.MOV`) survive migration and `media_key`; keys are never case-folded (case-sensitive backends).
- A key is untrusted data: `resolve_key_under` rejects absolute, `..`, backslash and any non-canonical shape before touching the filesystem (path traversal via a tampered `media.path`).
- Row whose `path` lacks its sha: untouched by 0006, counted in the `Store.open` WARNING, replay skips its payload with `SkipAndRecord`; never rewritten by guesswork.
- NULL `media.path` (schema allows it): counted as unnormalised; never dereferenced.
- Legacy payload with a foreign ext form (`.tar.gz` vs `.gz`): the key takes whatever follows the sha in the stored value — identical to the on-disk name, so the resolved file exists.
- `_guess_ext` receives a hostile/odd `DocumentAttributeFilename` suffix (space, control char, >16 chars): falls back to `""` with a WARNING; the phase continues.
- Migration re-run/idempotency: the canonical-compare predicate makes a second application a no-op; `schema_migrations` prevents it anyway.
- `status`/`export`/`collect` on an old store all apply 0006 on open (as prior migrations did); reproject's `ReplaySource` opens the source `mode=ro` and never migrates it — the source's `media`/`custody_log` stay legacy until the operator opens it with `Store.open`; its raw is what replay reads, so the out store is key-form regardless.
- A live `collect` after migration dedups via `_load_content_index` values that are now keys, so new `custody_log` rows for old files are key-form too (assert in `test_cross_run_dedup_uses_persisted_media_table`).
- Same profile dir reached through a symlink (the smoke's `$S/default/media`): `resolve_key_under` never resolves symlinks, so the count/size check proves reproject wrote nothing.
- Windows-style legacy values (backslashes) normalise correctly because the anchor is the sha, not a separator.
