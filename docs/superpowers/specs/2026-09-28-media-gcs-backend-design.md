# Media storage backend: write downloads to a GCS bucket (#63)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #63.
**Batch:** 2. **Depends on:** #62 (keys) and #64 (streaming sink) — both
must be on the base branch. Independent of #68.
**ADR:** lands `docs/adr/0008-media-backends.md` (the backend abstraction,
the dependency choice) and an amendment to ADR-0003 (the outbound allow-list)
before code. Security reviewer runs on this feature.

## 1. Goal

Let a collector host with little disk (the OSINT VM: 38 GB free) put media
straight into `gs://<bucket>/<prefix>/` instead of `data/<profile>/media/`,
keeping content addressing, create-only semantics and chain of custody.

## 2. The constraint that shapes the design: the bucket can't delete

The target bucket has **versioning on and a 365-day retention policy** (set
2026-09-28 for chain of custody). Deleting a live object there only turns it
into a noncurrent version, and no version can be permanently destroyed until
it is 365 days old (verified on this bucket on 2026-09-28). So the "upload to
`incoming/<uuid>`, server-side copy to the final key, delete the temp"
pattern from #63's first draft would keep — and bill — a second copy of every
file for a year (~150 GB of duplicates for the pending pull). Re-verify the
bucket's policy before implementing; this section assumes it unchanged.

**Therefore: only ever write an object once, to its final key.** The final
key needs the sha256, which is known only after the whole file has streamed.
So:

1. Stream the download into a **local temp file** via #64's `MediaSink`
   (disk use is bounded by the *largest single file* — 2.4 GB in the pending
   list — not by the total).
2. With the sha known, `exists(key)`? → dedup path, delete the local temp.
3. Otherwise upload the temp file to the final object name with
   **`if_generation_match=0`** (create-only: fails if any live object has that
   name — a lost race is treated as dedup, never an overwrite), then verify
   the server's CRC32C against a CRC32C computed locally while streaming
   (`google-crc32c`), then delete the local temp.
4. A verification mismatch is a hard failure for that file: log both
   checksums, record no `media`/custody row, raise `SkipAndRecord` — and
   **leave the object in place** (paperboy has no delete path, and a delete
   would only make it a retained noncurrent version). A later write of the
   same key is refused by design (create-only), so the file is reported as
   needing operator attention; the ADR documents the manual recovery.

## 3. Design

- **`MediaStore` Protocol** (`src/paperboy/media_store.py`):
  `exists(key) -> bool`, `commit(temp_path, key, crc32c) -> None`
  (create-only), `open_read(key) -> BinaryIO` (streaming read, used by
  replay), `describe() -> str` (for logs/custody, e.g. `gs://bucket/prefix`).
  - `LocalMediaStore(profile_dir)`: today's behaviour behind the interface
    (`commit` = `os.replace`, `exists` = `Path.exists`).
  - `GcsMediaStore(bucket, prefix)`: object name = `f"{prefix}/{key}"`.
- **Selection:** new setting `media_store: str | None` (`PAPERBOY_MEDIA_STORE`)
  — unset = local; `gs://<bucket>/<prefix>` = GCS. Validated at load; the
  bucket name is also checked against an explicit allow-list setting so a
  typo can't write evidence to someone else's bucket.
- **Collectors** (`media.py`, and avatars in `profiles.py` via the same
  store) talk only to `MediaStore`; they no longer touch the media dir
  directly. Temp files stay in a local scratch dir
  (`<profile_dir>/.incoming/`) for both backends.
- **Custody:** add a `store` column to `custody_log` (migration) recording
  `describe()` at write time — the key alone no longer says where the bytes
  are. `media.path` stays the backend-neutral key.
- **Credentials:** Application Default Credentials only (the VM's service
  account). No key files, nothing in the keychain. `doctor` gains a check:
  when a GCS store is configured, the bucket is reachable and a
  `testIamPermissions` for `storage.objects.create`/`get` passes — and
  **warns if `storage.objects.delete` is granted** (least privilege).
- **Allow-list (ADR-0003 amendment):** `storage.googleapis.com` is permitted
  only when a GCS store is configured, only for the configured bucket.
  Decide in the ADR whether GCS traffic uses the Telegram proxy: the opsec
  concern in `docs/opsec.md` is Telegram seeing the collector's IP; GCS
  traffic from a GCP VM stays on Google's network, so a direct path is the
  likely answer — but it's the ADR's call, argued explicitly.
- **Dependency:** `google-cloud-storage` (resumable uploads, ADC,
  `if_generation_match`) vs. the JSON API over `httpx` + `google-auth`.
  Recommend the former for correctness of resumable upload; the ADR records
  the choice.
- **Replay / reproject — open question for the planner.** Reproject has a
  hard **zero-network** invariant. Today `RawReplayGateway.download_media`
  reads the stored bytes back; with a GCS store that would be a network read.
  Recommended resolution: replay does not need the bytes at all — the raw
  `MediaDownload` payload already records `sha256`/`size`, and the object is
  content-addressed and create-only. So replay should report the recorded
  sha/size to the collector and the collector's `exists(key)` dedup path
  records custody without reading bytes. That changes what reproject
  verifies (it no longer re-hashes files), so it needs an explicit decision —
  and an offline `paperboy verify-media` could be the replacement integrity
  check (out of scope here; file it if chosen).

## 4. Tests (write first, see them fail)

- One `MediaStore` contract test suite run against `LocalMediaStore` and an
  in-memory GCS fake that enforces `if_generation_match=0` and a
  delete-refusing retention mode.
- Create-only: second commit of the same key → treated as dedup, no error,
  no second object.
- CRC32C mismatch → no rows, `SkipAndRecord`, object left, loud log.
- Never deletes: assert the GCS fake saw zero delete calls across the whole
  suite.
- Bucket not in the allow-list → config error at load.
- `doctor` permission check (fake `testIamPermissions`).
- Reproject with a GCS-configured source performs zero network I/O (patch
  the GCS client constructor to raise).

## 5. Ops prerequisites (operator, not code)

- Grant the VM service account `roles/storage.objectCreator` on the bucket
  (it has `objectViewer` today). **Do not** grant `objectAdmin` — no delete.
- The VM's access scope is `storage-ro`; changing it to read-write needs a
  VM stop/start. (Note the VM's ephemeral IP changes on restart.)
- Telegram egress from the VM needs a proxy per `docs/opsec.md` before any
  live run there.

## 6. Definition of done (smoke on real data)

On the VM with a test prefix (e.g. `paperboy/smoke-<date>/` — objects
written there are retained for 365 days; keep it to two small files):

```
PAPERBOY_MEDIA_STORE=gs://<bucket>/paperboy/smoke-<date> \
  uv run paperboy collect @<channel> --phases channel,media --media-msgs <photo-id>,<small-video-id>
gcloud storage ls -l gs://<bucket>/paperboy/smoke-<date>/media/**
```
Paste both outputs, the `custody_log` rows (showing `store`), the doctor
output, and a re-run showing `duplicates` with no upload.

## 7. Out of scope

Migrating the existing local archive into the store (the bucket already
mirrors it by hand), parallel uploads, lifecycle rules, any delete path.
