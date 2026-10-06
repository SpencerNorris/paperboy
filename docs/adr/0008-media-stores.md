# ADR-0008: Media stores: local or a write-once GCS bucket

## Status

Accepted (2026-10-06, #63). Amends ADR-0003 (outbound allow-list).

## Context

Until now every downloaded file landed under `data/<profile>/media/`. The
operator wants a run to be able to send its media to a GCS bucket with no
local copy. The bucket (read 2026-10-06, `gcloud storage buckets describe`):
versioning on, soft delete 604,800 s, **unlocked retention 8,035,200 s (93
days)** since 2026-09-28, uniform access, public-access prevention. Deleting
or overwriting a live object keeps the old bytes for the retention span, so
paperboy must write each object once, to its final name, and must have no
delete path at all. The final name contains the sha256, known only after the
whole file has streamed.

## Options considered

- **Client library.** (a) JSON API over `httpx` + `google-auth`; (b)
  `google-cloud-storage`. Chosen **b**: resumable uploads, ADC,
  `if_generation_match` and crc32c verification are built in and retried
  correctly. Cost: ~2.7 s cold import, so it is imported lazily in the one
  client factory.
- **Test double.** (a) `fake-gcs-server`; (b) an in-memory fake. Chosen **b**:
  no daemon, no network, and the fake can assert the create-only precondition
  and count deletes (must stay zero).

## Decision

- `paperboy.media_store.MediaStore` is the only seam: `store_id`, `exists`,
  `find_key`, `commit(temp, key, crc32c) -> bool` (create-only; `False` = lost
  race), `open_read`. `LocalMediaStore(profile_dir)` is today's behaviour;
  `GcsMediaStore(bucket, prefix)` has object name `<prefix>/<key>` and
  `store_id = gs://<bucket>/<prefix>`. `build_media_store` is the one
  constructor.
- A run has exactly one store, selected by `media_store`
  (`PAPERBOY_MEDIA_STORE`, `--media-store`), validated at load and required to
  be in `media_store_buckets`. Never both.
- Bytes stream into `<profile>/media/.incoming/<uuid>.part` (same temp
  location for both stores; same-filesystem rename for local). The free-disk
  floor measures that volume, a bucket run's only local disk use.
- The store is recorded per file: `custody_log.store` (migration 0007; `'local'`
  or the `gs://` URL, never a path) and a `"store"` key in `MediaDownload`/
  `AvatarDownload` payloads. **Absence of `store` in a receipt means local**:
  local-run receipts omit it so legacy and local raw logs stay byte-identical.
  `media.path` stays the store-neutral key.
- Every bucket run appends one paperboy-authored `MediaStore` raw record
  `{"store": "<url>"}` before the media collector, so a custody row that only
  records a duplicate still has its store derivable from raw. Local runs never
  write it. Replay pins it per run.
- "Already have it" is per store: a custody row naming this store, else
  `store.exists(key)`. `media.sha256` stays the PK, so a file first stored
  locally is downloaded again in a bucket run, committed under the existing
  row's key, with custody and a receipt but no second `media` row.
- crc32c is computed while streaming (`MediaSink`) and compared with the
  server's after upload. A mismatch (or `DataCorruption`) raises
  `MediaStoreIntegrityError`: both digests logged at ERROR, no rows, the file
  counted `skipped`, the object left in place. **Manual recovery:** compare
  `gcloud storage hash` with `media.sha256`; the operator lifts the unlocked
  retention and deletes by hand. Paperboy never will.
- Transport failures after the library's retries raise `MediaStoreError`,
  which stops the media phase (`PhaseStop`); a bucket outage is not per-file
  and never causes a re-download. Uploads happen outside `Budget.call`.
- Reproject network exception: bucket receipts are read with ranged GETs via
  `open_read`, only for buckets in the allow-list, re-hashing against the
  receipt sha. No Telegram, no keychain, no writes. A receipt for an unlisted
  bucket, missing ADC or a missing object is a per-file skip. A local-only
  source builds no GCS client. `--out-profile` copies into the local profile.
- `doctor` and the `collect`/`fetch-media` preflight check credentials,
  `storage.objects.create`/`get`, warn if `storage.objects.delete` is granted
  (expected on the operator's Mac), and print retention/versioning (a warning
  if `storage.buckets.get` is not granted).

## Consequences

- Evidence in the bucket is write-once; mistakes are recoverable only by an
  operator lifting retention.
- A bucket dry run of `fetch-media` touches GCS (metadata GETs), never
  Telegram.
- No archive migration, no parallel uploads, no copy-into-bucket from reproject.
- #91 builds on `MediaStore` as the only seam.

## Notes

Spec: `docs/superpowers/specs/2026-09-28-media-gcs-backend-design.md`. Plan:
`docs/superpowers/plans/2026-10-06-media-stores.md`.
