# Plan: per-run media stores — local or a GCS bucket (#63)
(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-10-06-media-stores.md` in your first commit.)

Branch `feat/media-stores` from `origin/dev/gcs-pull` (4323985); PR targets `dev/gcs-pull`. Authoritative: the spec
`docs/superpowers/specs/2026-09-28-media-gcs-backend-design.md`; the overview's live-smoke protocol and docs DoD bind
you. `file:line` cites 4323985. In anything committed: `gs://<bucket>/<prefix>`, `@<channel>`, `<id>`, `<scratch>`,
`<data-dir>` — never the bucket name, a channel handle/id or a machine path. #91 follows: `MediaStore` is the only seam.

## 1. Verified facts (2026-10-06, scratch venv; re-verify with `uv run python -c ...` after `uv add`)

- **Not yet a dependency:** `uv.lock` has no `google*` package. `uv add google-cloud-storage google-crc32c` gives
  google-cloud-storage 3.16.0 (+ google-auth, google-api-core, requests) and google-crc32c 1.9.0 with the C
  implementation on 3.12 and 3.14 (`google_crc32c.implementation == "c"`; assert it in a test — pure Python is 100x
  slower). Cold `import google.cloud.storage` costs ~2.7 s: import it **lazily** in the client factory only.
- **API (3.16.0):** `Client()` reads ADC via `google.auth.default()`; missing ADC raises
  `google.auth.exceptions.DefaultCredentialsError` at construction. `client.bucket(name).blob(object_name)`;
  `blob.exists()` = one metadata GET (the "HEAD"); `blob.upload_from_filename(path, if_generation_match=0,
  checksum="crc32c")` is multipart ≤ 8 MiB and resumable above (100 MiB chunks), retried on transient errors *because*
  `if_generation_match` is set (`DEFAULT_RETRY_IF_GENERATION_SPECIFIED`); an existing live object raises
  `google.api_core.exceptions.PreconditionFailed` (412); a final-chunk crc mismatch raises
  `google.cloud.storage.exceptions.DataCorruption` with the object already created; after success `blob.crc32c` is
  the base64 big-endian digest, comparable to `base64.b64encode(google_crc32c.Checksum(data).digest())`. Read:
  `blob.open("rb", chunk_size=1 << 20)` → ranged GETs (`checksum=None` on ranges — replay's sha re-hash is the
  integrity check); missing object → `google.api_core.exceptions.NotFound`. `bucket.test_iam_permissions([...])`
  returns the granted subset (no permission needed). `bucket.reload()` → `bucket.retention_period` (s) /
  `bucket.versioning_enabled` needs `storage.buckets.get`: the Mac owner has it, the VM service account will not.
- **Bucket (read-only describe):** us-east4, versioning on, soft-delete 604,800 s, **unlocked retention 8,035,200 s
  (93 d) since 2026-09-28**, uniform access, public-access prevention. Only `paperboy/default/` exists. ADC token: ok.

## 2. Ambiguities and resolutions

- **A1 Store seam.** `src/paperboy/media_store.py`: `MediaStore` Protocol = `store_id: str`, `exists(key) -> bool`,
  `find_key(sha) -> str | None` (#62's legacy-suffix reuse, `media_keys.py:57-73`, per store: local scans the shard
  dir; GCS lists `prefix/media/<xx>/<sha>` once, miss path only), `commit(temp: Path, key, crc32c_b64) -> bool`
  (create-only; `False` = lost race), `open_read(key) -> BinaryIO`. `LocalMediaStore(profile_dir)`: `exists` = stat,
  `commit` = today's `_finalize` (`media.py:116-119`) but `if dest.exists(): return False`, `store_id="local"`.
  `GcsMediaStore(bucket, prefix, client_factory)`: object name `f"{prefix}/{key}"`, `store_id=f"gs://{bucket}/
  {prefix}"`, client built lazily on first use. `build_media_store(settings, profile, *, client_factory=None)` is the
  one constructor callers use.
- **A2 Where the collector gets the store.** Not a new `CollectContext` field: both collectors already derive the
  profile dir from `(ctx.settings, ctx.profile)` (`media.py:250,406,415`, `profiles.py:647`) and now call
  `build_media_store(ctx.settings, ctx.profile)`. Tests inject the fake via `monkeypatch.setattr("paperboy.media_store.
  default_client_factory", lambda: fake_client)` — the one place that imports `google.cloud.storage`. Replay-without-
  copy (A6) never constructs a store.
- **A3 Per-store dedup in `media.py:333-342` (content-id hit) and `:385-395` (sha hit).** `media.sha256` is the PK, so
  "the DB has it" ≠ "this store has it". Content-id hit → `(sha, key)` → `stored_in(conn, sha, store_id)` (a
  `custody_log` row names this store; offline, no HEAD) else `store.exists(key)` → if either: custody only
  (`store=store_id`), `duplicates`; else download. After download, sha in `media` → the same two checks → custody only;
  else `commit` the temp under the existing row's key (store-neutral) and write custody + a `MediaDownload` receipt,
  **no second `media` row** (`INSERT ... ON CONFLICT(sha256) DO NOTHING`), counted `downloaded`. New sha: `key =
  media_key(sha, ext)`, `store.find_key(sha)` may substitute a legacy-suffixed key, then `commit`. A lost race (`False`)
  is logged INFO and continues exactly like a create (the bytes are verifiably there). Temp cleanup stays at `:431-435`.
- **A4 Receipts.** `MediaDownload`/`AvatarDownload` payloads gain `"store": "<store_id>"` **only when not local**
  (`media.py:437-440`, `profiles.py:651-654`): legacy and local-run receipts stay byte-identical (the round-trip
  contract compares `raw_records`, `reproject.md:599-611`); replay reads `payload.get("store", "local")`.
  `custody_log.store` is always written (`'local'` or the URL; the migration default backfills old rows).
- **A5 Run marker.** A dedup-only bucket run writes custody rows whose `store` nothing in raw records — raw-first
  needs a marker. The recipe appends one paperboy-authored `MediaStore` record `{"store": "<url>"}` right before the
  `media` collector, beside `MediaSelection` (`recipes.py:178-200`), **only when the store is not local** (local raw
  logs unchanged). Replay: `ReplaySource.media_store(run)` (clone `media_selection`, `replay.py:701-706`), pinned like
  `_pin_selection` (`reproject.py:227-241`), and `replay_settings.media_store = marker["store"]` (`:378-390`).
- **A6 Replay.** Collectors in replay-without-copy never touch a store: the gateway's served bytes prove existence;
  rows carry `store_id = settings.media_store or "local"`; the `path.exists()`/`find_existing_key` branch at
  `media.py:410-428` goes for that mode (the "file missing" skip comes from the gateway). `ProfilesCollector` gains
  `copy_on_replay` like `MediaCollector` (`reproject.py:533`): `--out-profile` still copies avatars into the local
  output profile; plain reproject writes nothing. `RawReplayGateway(source, clock, run, *, allowed_buckets)`:
  `download_media` (`replay.py:1018-1046`) and `download_user_photo` (`:1167-1180`) branch on the receipt's store:
  local → today's path; bucket ∉ allow-list → `log.warning` + `SkipAndRecord`; else a cached `GcsMediaStore.open_read
  (key)` copied in `_CHUNK` pieces into the sink, sha re-verified; `NotFound` → `SkipAndRecord`. A local-only source
  still builds no GCS client (test).
- **A7 fetch-media offline `already_stored` (`media_list.py:281-286`).** `classify_rows(..., media_store)` (required
  kwarg; `cli.py:533` passes `build_media_store(settings, profile)`): a candidate with a known `(sha, key)` is
  `already_stored` iff `stored_in(conn, sha, media_store.store_id)` or `media_store.exists(key)`, else `pending` (keep
  `stored` as a hint). The custody fast path keeps existing tests green (0007 backfills `'local'`) and bounds a bucket
  dry run to one HEAD per candidate no custody row already places in the bucket. Document: a bucket dry run touches
  GCS (metadata GETs), never Telegram.
- **A8 Temp location and floor.** Spec §2 says `<profile>/.incoming/`; keep today's `<profile>/media/.incoming/`
  (`media.py:70,80-85`): same-filesystem rename for local, `media_audit.py:18` already skips it, one location for both
  stores. The floor (`media.py:356-365`) keeps measuring that volume — a bucket run's only local disk use. "Local
  `media/` unchanged" in the DoD means no files outside `.incoming/`.
- **A9 Avatars (`profiles.py:645-650`).** Bytes → `media/.incoming/<uuid>.part` → `store.exists(key)` else
  `store.commit(temp, key, crc)` → unlink temp. Local gains an atomic write; GCS gets the same create-only path.
- **A10 CRC and commit errors.** `MediaSink` (`media_sink.py:75,87,99`) also feeds a `google_crc32c.Checksum`;
  `MediaSink.crc32c` is the base64 digest. `GcsMediaStore.commit` uploads with `if_generation_match=0,
  checksum="crc32c"`, then compares `blob.crc32c` with the sink's value: mismatch (or `DataCorruption`) raises
  `MediaStoreIntegrityError(local, remote, key)`; the collector logs both digests at ERROR, writes no rows, counts
  `skipped`, continues (per-file skip); the object stays (no delete path); the ADR documents manual recovery (`gcloud
  storage hash` vs `media.sha256`; the operator lifts the unlocked retention and deletes by hand; paperboy never will).
  Transport failures after the library's own retries (`GoogleAPICallError`, `requests.exceptions.RequestException`,
  `DefaultCredentialsError`) raise `MediaStoreError` → the collector raises `PhaseStop("media: cannot write to the
  media store: ...")` like `MediaSinkWriteError` (`media.py:373-379`): a bucket outage is not per-file and never causes
  a re-download. Uploads happen outside `Budget.call`; `Budget` sees only Telegram RPCs.
- **A11 Settings (`config.py:103-159`).** `media_store: str | None = None` (`PAPERBOY_MEDIA_STORE`, `--media-store`
  on `collect` and `fetch-media`) and `media_store_buckets: str = ""` (`PAPERBOY_MEDIA_STORE_BUCKETS`, comma-separated;
  property `media_store_bucket_set`). `@model_validator(mode="after")`: `media_store` must match `gs://<bucket>/
  <prefix>` (bucket `[a-z0-9][a-z0-9._-]{1,221}`, prefix non-empty, no leading/trailing `/`, `//` or `..`), trailing `/`
  stripped, bucket in the set, else `ValueError`. `cli.py` gets `_load_settings_or_exit` (`pydantic.ValidationError` →
  red message, exit 1) for `doctor`, `collect`, `fetch-media`, `reproject` (`cli.py:78-80,139,275,518,705`).
  `seg_settings.model_copy` (`fetch_media.py:131-137`) inherits the store.
- **A12 Doctor (`doctor.py:106-120`).** `media_store_checks(settings, client_factory) -> list[Check]`, sync, appended
  by `run_doctor` when `settings.media_store` is set: `media_store_credentials` (fail on `DefaultCredentialsError`),
  `media_store_permissions` (fail unless `storage.objects.create` and `.get` granted), `media_store_least_privilege`
  (**warn** if `storage.objects.delete` granted — expected on the Mac), `media_store_retention` ("retention N s (D days),
  versioning on/off"; warn "not readable (storage.buckets.get)" on 403). The `collect`/`fetch-media` preflight runs
  them too, so a bad bucket blocks before any Telegram download. Nothing here is recorded in raw.
- **A13 GCS fake (`tests/fake_gcs.py`).** In-memory `FakeGcsClient.bucket(name)` → `FakeBucket` (`blob(name)`,
  `list_blobs(prefix=)`, `test_iam_permissions(perms)` → configured subset, `reload()`, `retention_period`,
  `versioning_enabled`, `delete_blob` → `AssertionError` + counter) and `FakeBlob` (`exists()`,
  `upload_from_filename(filename, if_generation_match=None, checksum=None, **_)` asserts `if_generation_match == 0`,
  raises `PreconditionFailed` if the name exists, stores bytes, sets `crc32c` (overridable via `bucket.corrupt_crc`),
  `open("rb", chunk_size=)` → `BytesIO`, `delete` → `AssertionError` + counter). Counters
  `calls["exists"|"upload"|"open"|"list"|"delete"]`; a session `conftest` fixture asserts `delete == 0` suite-wide.
- **A14 Reproject needing ADC.** `reproject` passes `allowed_buckets=settings.media_store_bucket_set` through
  `_replay_one` to `RawReplayGateway`; a bucket receipt with no ADC fails that file with a `SkipAndRecord` (logged once
  per store), never a reproject abort. `--out-profile` of a bucket source copies **into the local output profile**.

## 3. Tasks (TDD: failing test → code → commit; gates green per task; commit after every green cycle)

Env, venv and `--basetemp` per the run rules. Gates: `uv run pytest -q --basetemp=<scratch>`, `uv run ruff check`, `uv run pyright`.

- **T0 ADRs + plan.** `docs/adr/0008-media-stores.md` (format of `0007-media-keys.md`): context (write-once bucket,
  the §1 retention/versioning numbers with the read date), options (JSON API over httpx + google-auth vs
  google-cloud-storage — chosen for resumable uploads, ADC, `if_generation_match`, crc32c; `fake-gcs-server` vs an
  in-memory fake — chosen in-memory), decision (A1–A14 in prose, incl. CRC manual recovery, the reproject network
  exception and its limits, lazy import), consequences. `docs/adr/0003-guardrails.md`: "Amendment (2026-10-06, #63)"
  after `:88`: `storage.googleapis.com` permitted only when a GCS store is configured or a receipt names an
  allow-listed bucket, only for those buckets, read-only for reproject; GCS is not Telegram traffic (Mac VPN / VM egress
  proxy) and does **not** use `settings.proxy` (MTProto/SOCKS) — argue it. Commit (with this plan): `docs(adr): ADR-0008
  media stores; ADR-0003 amendment for GCS egress (#63)`.
- **T1 Dependency + sink crc.** `tests/test_media_sink.py::test_sink_exposes_crc32c_base64` (chunks → equals
  `base64.b64encode(google_crc32c.Checksum(data).digest()).decode()`; `reset()` restarts it), `::test_crc32c_uses_the_c_
  implementation`. Code: `uv add google-cloud-storage google-crc32c`; `MediaSink` keeps a `google_crc32c.Checksum`
  beside the sha. Commit: `feat(media-sink): crc32c alongside sha256; add GCS deps (#63)`.
- **T2 Migration 0007.** `tests/test_store_migrations.py::test_0007_custody_log_store_column` (`0007_media_stores`
  applied; `custody_log.store` NOT NULL default `'local'`; a pre-existing row reads `'local'`). SQL: `ALTER TABLE
  custody_log ADD COLUMN store TEXT NOT NULL DEFAULT 'local';` + header comment. Commit: `feat(store): 0007 custody_log.store (#63)`.
- **T3 Settings.** `tests/test_config.py::test_media_store_defaults_env_and_validation` (unset → None; listed bucket
  ok, trailing slash stripped; unlisted bucket → `ValidationError` naming it; `file:`/missing prefix/`..` → error) and
  `::test_media_store_cli_beats_env`. Code: A11. Commit: `feat(config): media_store + media_store_buckets allow-list (#63)`.
- **T4 Store contract + fake.** `tests/fake_gcs.py` (A13). `tests/test_media_store.py` parametrised over
  `LocalMediaStore(tmp)` and `GcsMediaStore("b", "p/x", client_factory=fake)`: `exists` false→true after commit;
  `commit` True then False (second commit: no error, one object, local file untouched); `open_read` streams back;
  `find_key` finds a legacy-suffixed object/file; `store_id` is `"local"` / `"gs://b/p/x"`; GCS: the fake saw
  `if_generation_match == 0`, zero deletes; `corrupt_crc` → `MediaStoreIntegrityError` with both digests, object
  present; `PreconditionFailed` → `False`; `default_client_factory` patched to raise is never hit. Plus
  `stored_in(conn, sha, store_id)`. Commit: `feat(media-store): MediaStore protocol, local + GCS stores, GCS fake (#63)`.
- **T5 Media collector.** `tests/test_collector_media_stores.py` (reuse `_ctx/_seed/_doc_msg` from
  `test_collector_media.py`): `test_bucket_run_uploads_and_keeps_no_local_copy` (fake holds `p/x/media/<xx>/<sha>.pdf`;
  `media.path` is the bare key; `custody_log.store` is the URL; receipt has `"store"`; no file under `<profile>/media`
  outside an empty `.incoming`); `test_local_run_receipt_has_no_store_key_and_custody_says_local`;
  `test_file_from_local_run_is_downloaded_again_in_bucket_run` (local collect, then a bucket collect of the same
  fixtures: one upload, custody with the URL, no second `media` row, counted `downloaded`);
  `test_file_already_in_bucket_is_duplicate_without_upload` (`exists` called, `upload == 0`, custody only);
  `test_custody_fast_path_skips_the_head`; `test_crc_mismatch_skips_file_writes_no_rows_logs_both_digests` (`caplog`
  ERROR carries both digests; `media`/`custody_log`/receipt counts 0; `skipped == 1`; object present);
  `test_store_transport_error_is_a_phase_stop_not_a_retry` (`ServiceUnavailable` → `PhaseStop`, one Telegram call);
  `test_lost_create_race_continues_as_downloaded`; `test_run_marker_written_only_for_bucket_runs`. Code: A3, A4, A5
  (recipe), A8, A10. Commit: `feat(media): per-store dedup, create-only commit, store in custody/receipts, run marker (#63)`.
- **T6 Avatars.** `tests/test_collector_profiles.py::test_avatar_goes_through_the_media_store` (bucket run: object in
  the fake, receipt has `"store"`, custody URL, no local file; local run: file present, no `"store"` key). Code: A9.
  Commit: `feat(profiles): avatars through MediaStore (#63)`.
- **T7 Replay + reproject.** `tests/test_replay_gateway.py::test_download_media_reads_bucket_receipt_through_open_read`
  (receipt `"store": "gs://b/p"`, fake object → sink sha matches; fake `calls` has `open` only, `upload == delete == 0`),
  `::test_bucket_receipt_outside_allow_list_is_skipped_with_warning`, `::test_legacy_receipt_replays_local`,
  `::test_missing_bucket_object_is_a_skip`; `tests/test_replay_people.py::test_download_user_photo_from_bucket`;
  `tests/test_reproject.py::test_reproject_of_bucket_run_rebuilds_store_rows` (bucket collect with the fake → reproject
  → `custody_log.store`, receipts, `MediaStore` marker round-trip via `assert_round_trip`), `::test_reproject_is_
  incapable_of_network_or_keychain` + `default_client_factory` → raise (`:409-424`), `::test_out_profile_copies_bucket_
  media_into_local_profile`. Code: A5, A6, A14. Commit: `feat(replay): read media from the store a receipt names (#63)`.
- **T8 fetch-media.** `tests/test_media_list.py::test_already_stored_is_per_store` (local-run rows: `pending` against
  an empty fake bucket; `already_stored` when the fake holds the key; `already_stored` with zero HEADs when a custody
  row names the bucket); `tests/test_fetch_media.py::test_second_run_against_bucket_is_already_stored`;
  `tests/test_cli_fetch_media.py::test_dry_run_against_bucket_heads_but_never_builds_a_gateway` (`build_gateway` raises;
  fake `calls["exists"] > 0`). Code: A7, `--media-store` on `fetch-media` (`cli.py:457-520`). Commit:
  `feat(fetch-media): already_stored is per store; --media-store (#63)`.
- **T9 Doctor + CLI.** `tests/test_doctor.py::test_media_store_checks_pass_warn_and_fail` (delete granted → warn only,
  `doctor_blocks` false; `create` missing → fail; `DefaultCredentialsError` → fail; 403 on reload → warn naming
  `storage.buckets.get`), `::test_doctor_without_media_store_adds_no_checks`; `tests/test_cli.py::test_collect_media_
  store_flag_and_bad_bucket_exit_1` (unlisted bucket → exit 1 with a message, no gateway built). Code: A12,
  `--media-store` on `collect` (`cli.py:176-275`), `_load_settings_or_exit`. Commit: `feat(doctor,cli): GCS store
  preflight; --media-store on collect (#63)`.
- **T10 Docs (§4), then the DoD transcripts in `docs/features/media-stores.md`.** Commits: `docs: media stores across
  the feature docs, README, CLAUDE, data-model, how-it-works, opsec (#63)`; `docs(media-stores): DoD gates and smoke (#63)`.

## 4. Docs (every item is DoD; list each under "## Docs updated")

New `docs/features/media-stores.md` (purpose; flags/env/allow-list; behaviour per A1–A14; bucket facts and the
retention consequence; ADC and least privilege; the reproject exception; limitations: no delete, no archive migration,
no parallel uploads, a bucket dry run touches GCS, CRC recovery is manual; DoD gates + transcripts).
`media-streaming.md:22-80` (temp for both stores, commit replaces rename, per-store dedup order). `reproject.md:155-196`
(Guardrails: bucket-read exception, allow-list, ADC, still no Telegram/keychain) + the fetch-media replay section.
`fetch-media.md:54-66` (`already_stored` per store). `README.md` rows `:141,143` (`--media-store`), config table
`:157-167` (`MEDIA_STORE`, `MEDIA_STORE_BUCKETS`), docs list `:260-265`, Safety (GCS egress rule). `CLAUDE.md`: Commands
synopsis, the "In progress on `dev/gcs-pull`" paragraph (#63 landed, #91 next), a settled-decision bullet for store
roots. `data-model.md:52-57` (`MediaStore` marker, `store` in receipts), `:144-158`, `:294-304` (`custody_log.store`).
`how-it-works.md` §3 `:46-62` (location = store root + key) and §6 `:129-168` (bucket run, no local copy, read-back).
`opsec.md` Network `:45-60` (GCS egress, ADC only) and Data at rest `:77-82` (least privilege, retention, never delete).

## 5. Definition of done — smoke from the Mac (offline first, then ≤ 5 live Telegram invocations)

Scratch: `.backup` of the real store into `<scratch>/default/paperboy.sqlite` (no `media/` symlink needed);
`PAPERBOY_DATA_DIR=<scratch>`, `PAPERBOY_REQUIRE_PROXY=false`, `--profile default`, `--max-rpc 60`, `--max-flood-sleep
60`, `PAPERBOY_MEDIA_STORE=gs://<bucket>/paperboy/smoke-<YYYYMMDD>`, `PAPERBOY_MEDIA_STORE_BUCKETS=<bucket>`. Before
every live command: STOP flag, live-call counter, VPN check (pasted). Unredacted transcripts stay in `<scratch>`.

Offline (no counter): (1) `gcloud storage buckets describe gs://<bucket>` → retention/versioning (name redacted).
(2) `paperboy fetch-media <list> --dry-run --media-store gs://…` on a 3-row list of the chosen messages → all
`pending`, no Telegram; the same dry run after the live pull → `already_stored`. (3) After the live pull:
`paperboy reproject --profile default --out <scratch>/reprojected.sqlite` (ADC present; reads the 3 objects back) →
the source-vs-output table, `custody_log` rows with `store`, the `MediaStore` marker count, a log with no
`TelethonGateway`/`Budget` lines.

Picking the files (offline, on the scratch DB; never the two private ids, never pasted) — a channel already in the
store, with a username and existing `media` rows, that still has un-downloaded media:
```
WITH have AS (SELECT message_uri uri FROM media WHERE message_uri IS NOT NULL
              UNION SELECT source_message_uri FROM custody_log WHERE source_message_uri IS NOT NULL)
SELECT m.channel_id, m.msg_id, m.media_kind, json_extract(m.media_json,'$.document.size') AS size FROM messages m
WHERE m.deleted_at IS NULL AND m.uri NOT IN (SELECT uri FROM have) AND m.channel_id NOT IN (<the two private ids>)
  AND m.channel_id IN (SELECT DISTINCT messages.channel_id FROM media JOIN messages ON media.message_uri=messages.uri)
  AND (m.media_kind='MessageMediaPhoto' OR (m.media_kind='MessageMediaDocument' AND size < 5000000))
ORDER BY m.channel_id, m.msg_id LIMIT 40;
```
(Verified 2026-10-06: several such channels have hundreds of un-downloaded photos and dozens of sub-5 MB documents.)
Take **two photos and one document < 5 MB from one channel**, by id.

Live (counter ≤ 5): (L1) `paperboy doctor --profile default` with the store env → table incl. the four
`media_store_*` rows (least-privilege warn expected). (L2) `paperboy collect <id> --phases channel,media
--media-msgs <id>,<id>,<id>` with the store env → `downloaded: 3`. (L3) the same again → `duplicates: 3`, no upload.
Then offline: `gcloud storage ls -l "gs://<bucket>/paperboy/smoke-<date>/media/**"` (3 objects), `gcloud storage
hash` of each vs `media.sha256`, `SELECT path, sha256, store FROM custody_log ORDER BY id DESC LIMIT 6`, `find
<scratch>/default/media -type f` empty, `.incoming` empty, the raw `MediaStore` marker. Two invocations are spare for a
stopped/pending retry — never to "get a clean run". PENDING/STOPPED per the run rules is not a code failure.

## 6. Edge cases and failure modes (each has a test above unless marked ops)

| Case | Behaviour |
|---|---|
| CRC mismatch / lost create race (412) | ERROR with both digests, no rows, `skipped`, object left, ADR recovery (ops) / `commit` → False, INFO, rows + receipt as `downloaded`. |
| Network failure mid-upload | library resumable retries (because `if_generation_match` is set); then `MediaStoreError` → `PhaseStop`, no re-download, temp unlinked. |
| Temp cleanup / floor | `finally` unlink always; sweep unchanged; floor measured on `<profile>/media` for both stores (A8). |
| Bucket outside the allow-list | `collect`/`fetch-media`/`doctor`: config error, exit 1 before any client; replay: WARNING + skip, never fetched. |
| Missing ADC | doctor `media_store_credentials` fail (blocks collect); reproject of a bucket receipt: per-file skip, logged once. |
| Missing `storage.buckets.get` (VM SA) / delete granted | retention warns, never blocks; delete warns once, never used (fake proves 0 deletes). |
| Legacy receipt / pre-0007 rows / legacy-suffixed object | replay as local; `store='local'` by migration default; `find_key` reuses the object. |

## 7. Operator decisions needed
- Confirm A8: the temp dir stays at `<profile>/media/.incoming/` (spec §2 wrote `<profile>/.incoming/`).
- Confirm A4: `"store"` is omitted from local receipts (legacy-compatible) rather than always written.
- Confirm the DoD reproject of the bucket run (a GCS read, no Telegram) is not counted against the 5 live invocations.

## Orchestrator decisions (2026-10-06, binding)

1. **A8:** the temp dir is `<profile>/media/.incoming/` for both stores.
2. **A4:** `store` is omitted from local runs' `MediaDownload` receipts. Absence means local, so ADR-0008 must state that rule explicitly, and the parity golden stays unchanged.
3. **A5:** approved. Every bucket run writes one paperboy-authored `MediaStore` raw marker before any media, so a custody row that only records a duplicate (no `MediaDownload`) still has a store derivable from raw. Local runs never write it.
4. **Live caps:** the read-back reproject (read-only GCS GETs, no Telegram) doesn't count against the 5 live Telegram invocations. It does still need the VPN up, and it makes zero bucket writes.
5. **Schema:** `custody_log.store` (migration 0007) is the only schema change; `media.path` stays the store-neutral key.
