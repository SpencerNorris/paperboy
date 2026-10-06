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
--media`, `fetch-media` and avatars (`--profiles`) all go through the same
store. Mac first, using the operator's own Application Default Credentials.

## Inputs

- `--media-store gs://<bucket>/<prefix>` on `collect` and `fetch-media`, or
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
  import. There is **no delete path** anywhere in the module.
- **Write once.** Bytes stream into `<profile>/media/.incoming/<uuid>.part`
  (both stores; `MediaSink` also computes a crc32c). With the sha known, a
  bucket commit uploads with `if_generation_match=0` (create-only; also what
  makes the library's resumable retries safe) and compares the server's crc32c
  with the local one. The temp file is always deleted. A lost create race
  (HTTP 412) is a duplicate, never an overwrite: the run carries on as a normal
  download.
- **Per-store "already have it".** `media.sha256` is the primary key, so "the
  DB has it" does not mean "this store has it". A hit (by Telegram content id,
  or by sha after download) is a duplicate only if a `custody_log` row names this
  store (offline, no request), else if `exists(key)` says so (one metadata GET).
  A file known from a local run is downloaded again for a bucket run and
  committed under the existing row's key: custody + a receipt, **no second
  `media` row**, counted `downloaded`. For a brand-new sha the store first looks
  for the exact key or a legacy-suffixed object (#62) and reuses it.
- **Where it is recorded.** `custody_log.store` (migration `0007`): `local` or
  the full `gs://` URL, never a path. `MediaDownload`/`AvatarDownload` receipts
  carry `"store"` **only for a bucket run; a missing key means local** (legacy and
  local-run raw stays byte-identical). A bucket run also appends one
  `MediaStore` marker `{store}` just before the media phase (local runs write
  none) so a dedup-only run's custody rows still have a store in raw.
  `media.path` stays the store-neutral key.
- **Errors.** CRC mismatch (or the library's `DataCorruption`):
  `MediaStoreIntegrityError`; both digests at ERROR, no `media`/custody/receipt
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
  `collect`/`fetch-media` preflight add four checks: `media_store_credentials`
  (fail), `media_store_permissions` (fail unless `storage.objects.create` and
  `get` are granted), `media_store_least_privilege` (**warn** if `delete` is
  granted: expected on the operator's Mac, never blocks), and
  `media_store_retention` (reports the retention span and versioning; a warning
  if `storage.buckets.get` is not granted, as for the VM service account). A
  bad bucket therefore blocks before any Telegram download.
- **fetch-media.** `already_stored` is per store; a bucket `--dry-run` makes
  read-only metadata GETs to GCS and never contacts Telegram (`fetch-media.md`).
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
- A bucket `fetch-media --dry-run` and a bucket `reproject` need ADC and touch
  GCS (not Telegram).
- `--unsafe` skips the doctor preflight, including the store checks.

## Definition of done

Gates (`uv run pytest -q --basetemp=…`, `uv run ruff check`, `uv run pyright`)
and the smoke transcripts follow (redacted; unredacted transcripts stay in the
operator's scratch directory, referenced by filename).

DOD-PLACEHOLDER
