# Media storage backend: write downloads to a GCS bucket (#63)

**Status:** refreshed 2026-10-06 for operator approval (replaces the
2026-09-28 draft). **Tracking:** issue #63. **Base:** `dev/gcs-pull` (= `main`
with #62, #64, #69, #70, #75, #84, #68 shipped).
**Order:** 1 of 2 (then #91 `fetch-from-list`).
**ADR:** lands `docs/adr/0008-media-stores.md` (the store abstraction, the
dependency choice, the reproject network exception) and an amendment to
ADR-0003 (the outbound allow-list) before code. **Security reviewer runs on
this feature.**

## 1. Goal

Each run chooses where its media goes:

- **Local run** (default, today's behaviour): files land in
  `data/<profile>/media/` on the machine running paperboy.
- **Bucket run:** files stream straight to `gs://<bucket>/<prefix>/media/…`
  and **no local copy is kept** (only a transient temp file per download).

The database always stays local; it is the system of record. Only media bytes
move. Both `collect --media` and `fetch-media` (renamed `fetch-from-list` in
#91) honour the run's choice, because both go through the same media
collector.

**Mac first.** The operator's Mac runs the pull to the bucket using the
operator's own Application Default Credentials. The OSINT VM runs the same
code after the ops steps in §7.

## 2. The bucket constraint: write once, never delete

The bucket `osint-sandbox-7381e9-paperboy` (us-east4) has versioning on, a
7-day soft delete, and an **unlocked retention policy of 8,035,200 s (93
days)** (read 2026-10-06; re-read it before the smoke and quote it in the
ADR). Deleting or overwriting a live object keeps the old version for the
retention span. So paperboy **only ever writes an object once, to its final
name, and has no delete path at all**. The final name needs the sha256,
which is known only after the whole file has streamed:

1. Stream the download into a **local temp file** via #64's `MediaSink`
   (`<profile_dir>/.incoming/<uuid>.part`). Disk use is bounded by the largest
   single file (≈2.4 GB in the pending list), not by the total.
2. With the sha known, ask the store `exists(key)`. If it does, it's a
   duplicate: delete the temp and write custody only.
3. Otherwise upload the temp to the final object with
   **`if_generation_match=0`** (create-only: fails if a live object has that
   name; a lost race is treated as a duplicate, never an overwrite). Verify the
   server's CRC32C against one computed locally while streaming
   (`google-crc32c`). Then delete the temp.
4. A CRC mismatch is a hard failure for that file: log both checksums, write
   no `media`/custody row, raise `SkipAndRecord`, and **leave the object in
   place**. A rewrite of the same name is refused by design, so the file is
   reported as needing operator attention. The ADR documents the manual
   recovery.

## 3. Design

### 3.1 `MediaStore`

New module `src/paperboy/media_store.py`, a Protocol:
`exists(key) -> bool`, `commit(temp_path, key, crc32c) -> None`
(create-only), `open_read(key) -> BinaryIO` (streaming read, used by replay),
and `store_id -> str`.

- `LocalMediaStore(profile_dir)`: today's behaviour behind the interface
  (`commit` = atomic `os.replace` from `.incoming`, `exists` = `Path.exists`).
  `store_id = "local"`.
- `GcsMediaStore(bucket, prefix)`: object name `f"{prefix}/{key}"`.
  `store_id = "gs://<bucket>/<prefix>"`.

### 3.2 Recording where a file lives

A file's full location is **store root + key**. The key
(`media/<xx>/<sha><ext>`, ADR-0007) never changes. What changes is the root:

- `store` = `"local"` for files in the profile folder. **Never** an absolute
  path: the profile folder must stay movable (#62).
- `store` = the full `gs://<bucket>/<prefix>` URL for bucket files. Bucket
  URLs are stable.

Recorded in three places:

- **Raw first:** the `MediaDownload` receipt payload gains `"store"`. Replay
  reads each file from the store its receipt names. Legacy receipts with no
  `store` mean `"local"`.
- `custody_log.store` (migration): which store this sighting's bytes are in.
- `media.path` stays the store-neutral key.

### 3.3 "Already have it" is per store

Today the media collector skips a download when the database already has the
file (by Telegram content id, then by sha). That's wrong once two stores
exist: a file downloaded in a local run would be skipped in a later bucket run
even though the bucket lacks it. New rule:

- The pre-download content-id check maps to a key, then asks **this run's
  store** `exists(key)` (one HEAD request for GCS). If yes, write custody only.
  If no, download.
- After download, the sha check asks this run's store `exists(key)` likewise.
- `fetch-media`'s offline `already_stored` classification becomes per store:
  in a dry run against a bucket store, it does one HEAD per candidate key (no
  Telegram contact). Document that a bucket dry run therefore touches GCS.

The operator's existing 34,153,238,030 bytes are already in both places
(the bucket mirror under `paperboy/default/media/` was made by hand and
matches the local folder byte-for-byte), so no backfill is needed: the
per-store `exists` check answers correctly for both.

### 3.4 Selection, credentials and guardrails

- **Setting:** `media_store: str | None` (`PAPERBOY_MEDIA_STORE`, and a
  `--media-store` flag on `collect` and `fetch-media`). Unset = local;
  `gs://<bucket>/<prefix>` = GCS. Validated at load. The bucket name must also
  appear in an explicit allow-list setting (`media_store_buckets`), so a typo
  can't write evidence to someone else's bucket.
- **Credentials:** Application Default Credentials only (the operator's
  `gcloud auth application-default login` on the Mac; the service account on
  the VM). No key files, nothing in the keychain, nothing logged.
- **doctor:** when a GCS store is configured: the bucket is reachable,
  `testIamPermissions` grants `storage.objects.create` and `get`, and it
  **warns if `storage.objects.delete` is granted** (least privilege). On the
  Mac the operator is a project owner, so this warning is expected there;
  print it once and don't block. It also prints the bucket's retention span.
- **Allow-list (ADR-0003 amendment):** `storage.googleapis.com` is permitted
  only when a GCS store is configured, and only for that bucket. GCS traffic
  is not Telegram traffic; on the Mac the system VPN covers all egress anyway.
  The ADR argues whether GCS should go through a configured proxy.
- **Dependency:** `google-cloud-storage` (resumable uploads, ADC,
  `if_generation_match`). The ADR records the choice over the JSON API with
  `httpx` + `google-auth`.

### 3.5 Collectors

`media.py` (and avatars in `profiles.py`, via the same store) talk only to
`MediaStore` and never touch the media folder directly. The free-disk floor
(#53) now guards the **temp** location for both stores; for a bucket run it is
the only local disk use.

### 3.6 Replay / reproject

The operator decided on 2026-09-28 that the no-network rule bends for bucket
reads.

- `RawReplayGateway.download_media` streams the file from the store named in
  its receipt (`open_read(key)`) into the sink and re-hashes it, exactly as it
  reads a local file today. A bucket source therefore needs ADC on the machine
  running reproject.
- **Still absolute:** reproject never touches Telegram (no session, no
  `TelethonGateway`, no `Budget`), never `t.me` or `web.archive.org`, never
  writes or deletes in any bucket.
- **Allowed:** read-only GETs to bucket stores named in receipts *and* present
  in the allow-list. A receipt naming a bucket not in the allow-list is
  skipped with a WARNING, never fetched.
- `--out-profile` copies (#70) keep writing to the local output profile.
  Copying into a bucket is out of scope.
- A source whose receipts are all local stays fully offline, as today.

## 4. Tests (write first, see them fail)

- One `MediaStore` contract suite run against `LocalMediaStore` and an
  in-memory GCS fake that enforces `if_generation_match=0` and refuses
  deletes.
- Create-only: a second commit of the same key → duplicate, no error, no
  second object.
- CRC32C mismatch → no rows, `SkipAndRecord`, object left, loud log.
- **Never deletes:** the GCS fake saw zero delete calls across the whole
  suite.
- Per-store dedup: a file stored in a local run is downloaded again in a
  bucket run; a file already in the bucket is not.
- `MediaDownload.store` and `custody_log.store` are `"local"` or the full
  `gs://` URL, never an absolute path. Legacy receipts replay as local.
- Bucket not in the allow-list → config error at load; a replay receipt naming
  an unlisted bucket → skipped with a WARNING.
- No local copy after a bucket run: the profile's `media/` is unchanged and
  `.incoming` is empty.
- `doctor` permission check (fake `testIamPermissions`), including the delete
  warning.
- Reproject with a bucket source reads only through `open_read` (the fake
  records reads only, zero writes or deletes) and still builds no Telegram
  client. Reproject with a local source does zero network I/O (patch the GCS
  client constructor to raise).

## 5. Definition of done (smoke on real data, from the Mac)

Under the overview's live smoke protocol (VPN check before each Telegram call,
`PAPERBOY_REQUIRE_PROXY=false`, a scratch `.backup` data dir, `--max-rpc 60`,
`--max-flood-sleep 60`, ≤ 5 invocations, ≤ 3 small files, channels already in
the store, never the split-out investigation). Use a **smoke prefix**: objects
there are kept for the 93-day retention span, so keep it to small files.

```
PAPERBOY_DATA_DIR=<scratch> PAPERBOY_MEDIA_STORE=gs://osint-sandbox-7381e9-paperboy/paperboy/smoke-<date> \
  uv run paperboy doctor --profile default
PAPERBOY_DATA_DIR=<scratch> PAPERBOY_MEDIA_STORE=gs://osint-sandbox-7381e9-paperboy/paperboy/smoke-<date> \
  uv run paperboy collect <channel id> --phases channel,media --media-msgs <photo-id>,<small-video-id>
gcloud storage ls -l "gs://osint-sandbox-7381e9-paperboy/paperboy/smoke-<date>/media/**"
```

Paste (redacted): the doctor output (with the retention span), the collect
table, the `gcloud` listing, `custody_log` rows showing `store`, the sha256 of
each object (`gcloud storage hash`) matching `media.sha256`, an empty
`.incoming`, an unchanged local `media/`, and a re-run showing duplicates with
no upload. Then an offline `reproject` of the scratch store that reads those
objects back (paste the source vs output table).

## 6. Docs

`docs/features/media-streaming.md` (stores), a new
`docs/features/media-stores.md`, `docs/features/reproject.md` (the network
exception), README (flag and config table), CLAUDE.md (command synopsis and
status), `docs/how-it-works.md` §3 and §6, `docs/data-model.md`
(`custody_log.store`), and `docs/opsec.md` (ADC, least privilege).

## 7. Running it on the VM (ops, not code)

The same code runs on the OSINT VM after:

- granting the VM service account `roles/storage.objectCreator` on the bucket
  (it has `objectViewer` today; **never** `objectAdmin`);
- changing its access scope from `storage-ro` to read-write (needs a VM stop
  and start; the ephemeral IP changes);
- giving Telegram egress a proxy or VPN per `docs/opsec.md`.

## 8. Out of scope

Migrating the existing local archive into the bucket (already mirrored by
hand), parallel uploads, lifecycle rules, copying into a bucket from
reproject, and any delete path.
