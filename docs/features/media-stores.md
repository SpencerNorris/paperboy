# Feature: per-run media stores (local or a write-once GCS bucket)

**Status:** implemented on `feat/media-stores` (#63; PR to `dev/gcs-pull`).
**Spec:** `docs/superpowers/specs/2026-09-28-media-gcs-backend-design.md`.
**Plan:** `docs/superpowers/plans/2026-10-06-media-stores.md`.
**ADRs:** `docs/adr/0008-media-stores.md`; amendment to
`docs/adr/0003-guardrails.md` (outbound allow-list). Follow-up: #91
(`fetch-from-list`) builds on `MediaStore` as the only seam.

## Purpose

Each run's media goes to **one** store, never both: the local profile folder
(the default), or a GCS bucket with **no local copy**. The database always
stays local and is the system of record; only media bytes move. `collect
--media`, `fetch-from-list` and avatars (`--profiles`) all go through the same
store. Mac first, using the operator's own Application Default Credentials.

## Inputs

- `--media-store gs://<bucket>/<prefix>` on `collect` and `fetch-from-list`, or
  `PAPERBOY_MEDIA_STORE`. Unset = local. Validated at load: the bucket name
  must be well formed, the prefix non-empty with no empty, `.` or `..`
  segments, and a trailing `/` is dropped.
- `PAPERBOY_MEDIA_STORE_BUCKETS`: comma-separated allow-list. The store's
  bucket must be in it (a typo cannot write evidence to someone else's
  bucket); it is also the set of buckets `reproject` may read receipts from.
  An invalid setting exits 1 with a one-line message naming the field (never
  the value).
- Credentials: **Application Default Credentials only** (`gcloud auth
  application-default login`; the service account on the VM). No key files,
  nothing in the keychain, nothing logged.

## Behaviour

- **The seam.** `src/paperboy/media_store.py`: `MediaStore` protocol
  (`store_id`, `exists`, `find_key`, `commit`, `open_read`),
  `LocalMediaStore(profile_dir)`, `GcsMediaStore(bucket, prefix)` (object name
  `<prefix>/<key>`, `store_id = gs://<bucket>/<prefix>`), and
  `build_media_store(settings, profile)`. `google.cloud.storage` is imported
  only inside `default_client_factory`, so local runs never pay its ~2.7 s
  import. There is **no delete path** anywhere in the module, nor in the client
  library calls it makes (see ADR-0008).
- **Write once.** Bytes stream into `<profile>/media/.incoming/<uuid>.part`
  (both stores; `MediaSink` also computes a crc32c). With the sha known, a
  bucket commit uploads with `if_generation_match=0` (create-only; also what
  makes the library's resumable retries safe) with our crc32c in the object
  metadata, so GCS rejects a mismatching upload without creating it; the
  server's crc32c is compared with the local one afterwards. (The library's own
  `checksum="crc32c"` is not used: on a resumable upload it issues a DELETE on a
  mismatch.) The temp file is always deleted. A lost create race
  (HTTP 412) is a duplicate, never an overwrite: the other writer's object is
  verified and kept, the rows and receipt are still written (this DB had no
  record of the bytes), and the file is counted under `duplicates`, not
  `downloaded`.
- **Per-store "already have it".** `media.sha256` is the primary key, so "the
  DB has it" does not mean "this store has it". A hit (by Telegram content id,
  or by sha after download) is a duplicate only if a `custody_log` row names this
  store (offline, no request), else if an existing object passes verification: its
  server crc32c must equal the streamed one (one metadata GET); with no stream to
  compare against, a bucket object is re-fetched rather than trusted.
  Any object found instead of written (exact key, legacy key, lost race) is
  verified the same way before it is adopted. A file known from a local run is
  downloaded again for a bucket run and
  committed under the existing row's key: custody + a receipt, **no second
  `media` row**, counted `downloaded`. For a brand-new sha the store first looks
  for the exact key or a legacy-suffixed object (#62) and reuses it.
- **Where it is recorded.** `custody_log.store` (migration `0007`): `local` or
  the full `gs://` URL, never a path. `MediaDownload`/`AvatarDownload` receipts
  carry `"store"` **only for a bucket run; a missing key means local** (legacy and
  local-run raw stays byte-identical). A bucket run also appends one
  `MediaStore` marker `{store}` before the first phase that writes through the
  store (`profiles` avatars or `media`; `"media": false` when the run has no
  media phase; local runs write none) so a dedup-only run's custody rows still have a store in raw.
  `media.path` stays the store-neutral key. Replay decides whether an avatar was
  fetched from the run's own `AvatarDownload` receipt (not from the projection),
  so an avatar re-fetched into a new store is reproduced, and a store outage while
  writing avatars stops the profiles phase (`PhaseStop`) like media.
- **Errors.** CRC mismatch (server rejection, or a stored object whose crc32c
  differs): `MediaStoreIntegrityError`; both digests at ERROR, no `media`/custody/receipt
  rows, the file is counted `skipped`, the object is left in place, the run
  continues. Manual recovery (ADR-0008): compare `gcloud storage hash` with
  `media.sha256`; the operator lifts the unlocked retention and deletes by hand;
  paperboy never will. A transport failure after the library's own retries
  (`GoogleAPICallError`, `RequestException`, credential errors) is
  `MediaStoreError`, which stops the media phase (`PhaseStop`); a bucket outage
  is not per-file and never causes a re-download. Uploads happen outside
  `Budget.call`; `Budget` only sees Telegram RPCs.
- **Free-disk floor** still measures the volume holding `media/.incoming/`:
  for a bucket run that is the only local disk use. `media/` itself stays empty.
- **Avatars** use the same path (temp file, `exists`, create-only commit); a
  known avatar already held by the run's store is not fetched again.
- **doctor / preflight.** With a store configured, `doctor` and the
  `collect`/`fetch-from-list` preflight add four checks: `media_store_credentials`
  (fail), `media_store_permissions` (fail unless `storage.objects.create` and
  `get` are granted), `media_store_least_privilege` (**warn** if `delete` is
  granted: expected on the operator's Mac, never blocks), and
  `media_store_retention` (reports the retention span and versioning; a warning
  if `storage.buckets.get` is not granted, as for the VM service account). A
  bad bucket therefore blocks before any Telegram download.
- **fetch-from-list.** `media_held` (outcome `already_stored`) is per store; a bucket `--dry-run` makes
  read-only metadata GETs to GCS and never contacts Telegram (`fetch-from-list.md`).
- **Reproject (the network exception).** Bucket receipts are read back with
  ranged GETs, only for allow-listed buckets, re-hashed against the receipt
  sha, no writes; see `reproject.md`, "Bucket reads". The media collector in
  replay never builds a store for a plain reproject (the gateway's served bytes
  prove existence and the receipt names the key); `--out-profile` copies into the
  LOCAL output profile.

## The bucket

Read 2026-10-06 (`gcloud storage buckets describe`, name redacted): location
`US-EAST4`, public access prevention enforced, uniform bucket-level access,
object versioning on, soft delete 604,800 s, **unlocked retention policy of
8,035,200 s (93 days)** since 2026-09-28. Deleting or overwriting a live object
keeps the old version for the retention span, so objects are written once and
small smoke files cost 93 days of retention.

## Limitations

- No delete path, by design; no overwrite; a CRC mismatch needs an operator.
- No migration of an existing local archive into a bucket (mirrored by hand
  already, so the per-store `exists` check answers correctly for both).
- No parallel uploads; no copy-into-bucket from `reproject`.
- A bucket `fetch-from-list --dry-run` and a bucket `reproject` need ADC and touch
  GCS (not Telegram).
- `--unsafe` skips the doctor preflight, including the store checks.

## Definition of done

Gates (`uv run pytest -q --basetemp=…`, `uv run ruff check`, `uv run pyright`)
and the smoke transcripts follow (redacted; unredacted transcripts stay in the
operator's scratch directory, referenced by filename).

### Gates (pasted tool output)

```
$ NO_COLOR=1 TERM=dumb uv run pytest -q --basetemp=<scratch>
1085 passed in 88.01s (0:01:28)
$ uv run ruff check
All checks passed!
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

(`NO_COLOR=1`: without it, 9 CLI-help assertions fail on ANSI colour codes in
the operator's terminal environment; that is not a code defect.)

Files in scope: the name-only diff against `origin/dev/gcs-pull` lists 48 files
(it was 46 at the first review, not 50). They are: `CLAUDE.md`, `README.md`, `docs/adr/0003-guardrails.md`,
`docs/adr/0008-media-stores.md`, `docs/data-model.md`,
`docs/features/{fetch-from-list,media-stores,media-streaming,reproject}.md`,
`docs/how-it-works.md`, `docs/opsec.md`,
`docs/superpowers/plans/2026-10-06-media-stores.md`, `pyproject.toml`,
`uv.lock`, `src/paperboy/{cli,config,doctor,fetch_from_list,media_list,media_sink,media_store,recipes,replay,reproject}.py`,
`src/paperboy/collectors/{media,profiles}.py`,
`src/paperboy/store/migrations/0007_media_stores.sql`, and the matching tests
(`tests/fake_gcs.py` is the in-memory GCS fake; `tests/conftest.py` asserts no
test ever attempts a bucket delete; `tests/fixtures/reproject/parity_golden.json`
gains only `"store": "local"` on each `custody_log` row).

### Smoke test transcript

(Historical: the transcripts below use the command's name at the time,
`fetch-media`, renamed `fetch-from-list` in #91, and its pre-#91 outcome words.)

Scratch data dir `<scratch>` (an `sqlite3 ".backup"` of the real store, never
the real store itself), `PAPERBOY_REQUIRE_PROXY=false`, `--profile default`,
`--max-rpc 60 --max-flood-sleep 60`, no `--unsafe`/`--join`/`--profiles`,
`PAPERBOY_MEDIA_STORE=gs://<bucket>/paperboy/smoke-20261006`,
`PAPERBOY_MEDIA_STORE_BUCKETS=<bucket>`. Target: two photos and one document
(< 100 KB each, 322,422 bytes total) from one channel `@<channel>` already in
the store, by id (`<id>`, `<id>`, `<id>`). Bucket writes only under the fresh
smoke prefix. Unredacted transcripts in `<scratch>`: `L1-doctor-unredacted.txt`,
`L2-collect-unredacted.txt`, `L3-collect-unredacted.txt`,
`reproject-mini-unredacted.txt`, `bucket-describe-unredacted.txt`,
`gcloud-ls-unredacted.txt`, `object-sha256-unredacted.txt`, `live-calls.log`.

**Which commit the live smoke ran on.** The live smoke below ran at `fad48a2`.
Later commits changed the upload path (server-side crc32c in object metadata,
verify-before-adopt, avatar replay) and the lost-race count (duplicate, not
downloaded). No second live smoke was run: the feature's cap of 3 media files in
the retained bucket is already spent, so a re-run (which would upload again)
would break it. The changed paths are covered offline by the fake-bucket tests
and `tests/test_media_store_real_library.py` (the real client library against a
stub transport, asserting create-only uploads and no DELETE).

**Live Telegram invocations: 3 of 5** (L1 doctor, L2 collect, L3 collect). The
live-call counter file holds 4 lines: the first line was written for an L1
attempt that never started (`timeout: command not found`, no Telegram contact)
and was kept rather than edited. STOP flag absent and the VPN check passed
before each (both addresses routed via `utun*`). No flood waits, no stop
condition.

VPN check before L1, L2 and L3 (identical each time):
```
149.154.167.51 -> utun*
91.108.56.130 -> utun*
```

Offline (1), the bucket (read-only describe, name redacted): `US-EAST4`,
`public_access_prevention: enforced`, retention policy `retentionPeriod:
'8035200'` effective 2026-09-28, soft delete `604800` s, uniform access.

Offline (2), `fetch-media <list> --dry-run --media-store gs://<bucket>/paperboy/smoke-20261006`
on the 3-row list, before the pull: `pending 3`, `already_stored 0`, no
Telegram, no keychain. After the pull: `already_stored 3`, `pending 0`.

L1, `paperboy doctor --profile default` with the store env (the four store rows):
```
media_store_credentials     ok    Application Default Credentials found
media_store_permissions     ok    storage.objects.create and get granted
media_store_least_privilege warn  storage.objects.delete is granted; paperboy never deletes, but a create-only role (roles/storage.objectCreator) is safer
media_store_retention       ok    retention 8035200 s (93 days), versioning on
```
(the account rows above them were all `ok`; `PASS`.)

L2, `paperboy collect <id> --phases channel,media --media-msgs <id>,<id>,<id>`:
```
channel {'channels': 1, 'peers': 1}
media   {'downloaded': 3, 'duplicates': 0, 'unavailable': 0, 'skipped_kind': 0, 'skipped': 0, 'size_mismatch': 0, ... 'too_large': 0}
media store: created media/<xx>/<sha>.jpg   (x3)
```

L3, the same command again (no upload):
```
media   {'downloaded': 0, 'duplicates': 3, 'unavailable': 0, 'skipped_kind': 0, 'skipped': 0, ...}
```

After the pull (offline):
```
$ gcloud storage ls -l "gs://<bucket>/paperboy/smoke-20261006/media/**"
    102265  2026-10-06T18:20:38Z  gs://<bucket>/paperboy/smoke-20261006/media/53/53954a4b71af....jpg
     92767  2026-10-06T18:20:40Z  gs://<bucket>/paperboy/smoke-20261006/media/60/602d9b566fb4....jpg
    127390  2026-10-06T18:20:36Z  gs://<bucket>/paperboy/smoke-20261006/media/cc/ccabee3cb6df....jpg
TOTAL: 3 objects, 322422 bytes (314.87kiB)
$ sqlite3 ... "SELECT substr(path,1,12), substr(sha256,1,12), store FROM custody_log ORDER BY id DESC LIMIT 6"
media/60/602...|602d9b566fb4|gs://<bucket>/paperboy/smoke-20261006      (x2 each of 3 files: L2 and L3)
media/53/539...|53954a4b71af|gs://<bucket>/paperboy/smoke-20261006
media/cc/cca...|ccabee3cb6df|gs://<bucket>/paperboy/smoke-20261006
$ find <scratch>/default/media -type f | wc -l          -> 0
$ ls -A <scratch>/default/media/.incoming | wc -l       -> 0
raw MediaStore markers (2 runs): {"store": "gs://<bucket>/paperboy/smoke-20261006"}
```
Integrity: `gcloud storage cat` of each object piped to `shasum -a 256` equals
`media.sha256` for all three (full 64-hex digests in `object-sha256-unredacted.txt`).
(`gcloud storage hash` reports md5/crc32c, not sha256, so the sha was recomputed
from the downloaded bytes.) The local `media/` is unchanged: zero files, empty
`.incoming/`.

Read-back, offline reproject (reads the 3 objects, no Telegram). A full reproject
of the whole scratch store was impractical in the time available (the machine was
I/O-contended and background jobs are throttled), so it ran on a reduced copy of
the scratch store: the same file with `raw_records` cut to the channel's history
run plus the two smoke runs, `--phases channel,history,media`, source profile
`mini`:
```
$ paperboy reproject --profile mini --phases channel,history,media --out <scratch>/reprojected-mini.sqlite
media: store local                       (the historical run)
media: store gs://<bucket>/paperboy/smoke-20261006
  media  downloaded=3 duplicates=0 ... not_selected=7790       (L2 replayed: 3 objects read back, sha re-verified)
media: store gs://<bucket>/paperboy/smoke-20261006
  media  downloaded=0 duplicates=3 ... not_selected=7790       (L3 replayed)
row counts - source vs reprojected (source = the unreduced backup's projections)
  raw_records 8751 -> 8723 (phases limited)   media 307 -> 3   custody_log 1841 -> 6
  messages 53554 -> 8400 (this channel only)
custody_log by store in the output:  gs://<bucket>/paperboy/smoke-20261006 | 6
raw MediaStore markers in the output: 2
```
Grep of the reproject console log and `.log` for `TelethonGateway`, `Budget`
or `rpc `: 0 matches. `gcloud storage ls` after the reproject: still 3 objects
(zero bucket writes).

Not covered by a live smoke: a CRC mismatch, a lost create race and bucket
outages (covered by `tests/test_media_store.py` and
`tests/test_collector_media_stores.py` with the in-memory fake); a VM run with a
create-only service account (ops, spec section 7).

### Override smoke at ce65395 (operator-approved 5th call)

The original smoke ran at `fad48a2`, before the server-side crc32c upload,
verify-before-adopt and replay changes. With the operator's one-off approval
(2026-10-06) this single extra invocation re-ran the upload path at `ce65395`:
live Telegram invocation 5 of 5, one photo (110,165 bytes, `<id>`, `@<channel>`)
to a new prefix `gs://<bucket>/paperboy/smoke-20261006b`. Scratch data dir
`<scratch>`, `PAPERBOY_REQUIRE_PROXY=false`, `--profile default`, `--max-rpc 20
--max-flood-sleep 60`, `PAPERBOY_MEDIA_STORE_BUCKETS=<bucket>`. STOP flag absent,
counter was 4 before the call, no flood wait. VPN check immediately before:
```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```
Unredacted transcripts in `<scratch>`: `L5-collect-unredacted.txt`,
`gcloud-ls-b-unredacted.txt`, `object-describe-b-unredacted.txt`,
`object-sha256-b-unredacted.txt`, `dryrun-b-unredacted.txt`,
`reproject-mini-b-unredacted.txt`, `live-calls.log`.

`paperboy collect <id> --phases channel,media --media-msgs <id>`:
```
media: store gs://<bucket>/paperboy/smoke-20261006b
media store: exists(media/63/63c0897f96dc....jpg) -> False
media store: created media/63/63c0897f96dc....jpg
media {'downloaded': 1, 'duplicates': 0, 'unavailable': 0, 'skipped_kind': 0, 'skipped': 0, 'size_mismatch': 0, 'out_of_window': 0, 'not_selected': 7792, 'too_large': 0}
```
RPC count for the run: 10 (account/channel preamble 1-8, `channels.getMessages`, `upload.getFile`).

Bucket, read-only:
```
$ gcloud storage ls -l "gs://<bucket>/paperboy/smoke-20261006b/**"
    110165  2026-10-06T22:07:53Z  gs://<bucket>/paperboy/smoke-20261006b/media/63/63c0897f96dc....jpg
TOTAL: 1 objects, 110165 bytes (107.58kiB)
$ gcloud storage objects describe <that object>      (relevant fields)
crc32c_hash: ROouXw==
size: 110165
retention_expiration: 2027-01-07T22:07:53+0000
content_type: application/octet-stream
(no custom metadata fields; the crc32c is the server-validated integrity value)
$ gcloud storage cp <object> <scratch>/dl-b/ ; shasum -a 256 <downloaded>
63c0897f96dc....   (equals media.sha256 for the row: 1)
$ gcloud storage hash <downloaded>      crc32c_hash: ROouXw==   (equals the object's)
```
Custody and receipt:
```
custody_log (newest): media/63/63c|63c0897f96dc|gs://<bucket>/paperboy/smoke-20261006b
raw MediaDownload receipt store: gs://<bucket>/paperboy/smoke-20261006b
raw MediaStore marker store:     gs://<bucket>/paperboy/smoke-20261006b
$ ls -A <scratch>/default/media/.incoming | wc -l      -> 0
$ find <scratch>/default/media -type f | wc -l         -> 0
```
`fetch-media <1-row list> --dry-run` with the same store env: `already_stored 1,
pending 0, total 1`; grep of its output for `pacing:`, `rpc `, `Gateway`,
`upload.getFile`: 0 matches (zero Telegram calls, no gateway built); the live-call
log stayed at 5 lines.

Offline `reproject` of a reduced copy (the scratch store's `raw_records` cut to
the channel history run plus the three smoke runs, `--phases channel,history,media`),
which read the object back from the bucket:
```
media: store gs://<bucket>/paperboy/smoke-20261006b
  media  downloaded=1 duplicates=0 ... not_selected=7792     (L5 replayed: object read back, sha re-verified)
table        source  reprojected   (source = the reduced copy's stale projections)
raw_records  8757    8730          (phases limited)
media        307     4             (3 earlier smoke files + this one; the full source has 308)
custody_log  1841    7             (6 earlier + 1)
custody_log by store in the output: <bucket>/paperboy/smoke-20261006 | 6 ; <bucket>/paperboy/smoke-20261006b | 1
```
Grep of the reproject console log and `.log` for `TelethonGateway`, `Budget` or
`rpc `: 0 matches. `gcloud storage ls` of the prefix after the reproject: still 1
object (zero bucket writes). The two prefixes hold 4 retained files in total, the
operator-approved override of the 3-file cap.

### Docs updated

`docs/features/media-stores.md` (new), `docs/features/media-streaming.md`,
`docs/features/reproject.md`, `docs/features/fetch-from-list.md`, `README.md`,
`CLAUDE.md`, `docs/data-model.md`, `docs/how-it-works.md`, `docs/opsec.md`,
`docs/adr/0008-media-stores.md` (new), `docs/adr/0003-guardrails.md`
(amendment), `docs/superpowers/plans/2026-10-06-media-stores.md`.

### Review notes

- No delete or overwrite path to GCS exists: `media_store.py` has no delete call;
  `commit` is `upload_from_filename(..., if_generation_match=0)`; the GCS fake
  raises `AssertionError` on any delete and `tests/conftest.py` asserts none was
  attempted over the whole suite.
- Tests never touch the network: they patch
  `paperboy.media_store.default_client_factory` or pass a fake factory.
- Logs carry exception class names only, never tokens or credentials.
