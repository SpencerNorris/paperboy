# Media storage batch — overview and run order

**Status:** draft for Gate A, 2026-09-28. **Operator goal:** pull ~146 GB of
catalogued media (1,620 posts, 11 channels, files up to 2.4 GB) into the
content-addressed archive and the GCS bucket, preferably from the OSINT VM
(2 vCPU, 7.7 GiB RAM, 38 GB free disk), falling back to the Mac.

This file is the index. Each feature has its own spec, written so it can be
handed to one implementing agent on its own.

| Spec | Issue(s) | Batch | Why it's needed for the pull |
|---|---|---|---|
| [`2026-09-28-media-relative-paths-design.md`](2026-09-28-media-relative-paths-design.md) | #62 | 1 | Stored locations must be portable before the store moves to the VM / media to GCS |
| [`2026-09-28-media-streaming-design.md`](2026-09-28-media-streaming-design.md) | #64, #53 | 1 | A 2.4 GB file must not be held in RAM; the disk must not fill silently |
| [`2026-09-28-pacing-safety-factor-design.md`](2026-09-28-pacing-safety-factor-design.md) | #69 | 2 (first) | Operator policy: double every assumed wait and every server-mandated wait |
| [`2026-09-28-media-list-fetch-design.md`](2026-09-28-media-list-fetch-design.md) | #68 | 2 (after #69) | The pull is driven by a cross-channel CSV, not one target |
| [`2026-09-28-media-gcs-backend-design.md`](2026-09-28-media-gcs-backend-design.md) | #63 | 2 (after #68, or in parallel with it) | The VM's disk is smaller than the pull |

## Run order

- **Batch 1 — `federated-run`, 2 features in parallel:** #62 and #64 on
  `dev/media-storage`. They share one file (`collectors/media.py`); the
  ownership split in §"Seams" below keeps the conflict to a few lines.
- **Batch 2:** #69 (pacing) first, then #68 and #63 (in parallel is fine —
  they touch different layers; see each spec's "Depends on").
- **Mac fallback** is possible after batch 1 + #68: the data volume had
  190 GiB free on 2026-09-28, so the full pull fits but leaves ~44 GiB; P1+P2
  (~90 GB) is comfortable. Set the #64 free-disk floor accordingly. **VM
  run** needs #63 plus the ops steps in the GCS spec.

## Seams between batch-1 features (read before implementing either)

Both features edit the write path in `collectors/media.py` (and the avatar
twin in `collectors/profiles.py`). To keep the parallel legs mergeable:

- **#62 owns naming and reading:** it introduces `media_key(sha256, ext) ->
  str` (`"media/ab/<sha><ext>"`) and `resolve_media_key(settings, profile,
  key) -> Path` in a new `src/paperboy/media_keys.py`, stores the key (not a
  path) in `media.path`, `custody_log.path` and new raw payloads, migrates old
  rows, and updates `replay.py`'s readers.
- **#64 owns writing:** it changes the gateway download signature to stream
  into a sink, adds the temp-file + atomic-rename writer and the disk guard.
  It keeps **today's** destination expression (`media_root / sha[:2] /
  f"{sha}{ext}"`) and today's `str(path)` stored value, untouched.
- **Integration (after both merge onto `dev/media-storage`):** replace #64's
  destination expression with `resolve_media_key(..., media_key(sha, ext))`
  and its stored value with the key. Both specs list this as the one expected
  conflict; the integrator resolves it and re-runs the full suite.

## Shared constraints (from CLAUDE.md; non-negotiable)

- Read-only against Telegram; every RPC goes through `Budget` — no collector
  calls the gateway raw.
- Raw first: `raw_records` stays append-only. Old payloads are never
  rewritten; the projection normalizes them.
- Storage is a settled decision: #62 and #63 each land an ADR (0007, 0008)
  before code.
- Tests: `uv run pytest -q` (point `TMPDIR`/`--basetemp` at a volume with
  free space — a near-full root disk causes spurious SQLite I/O errors),
  `uv run ruff check`, `uv run pyright` — all green per task. Definition of done includes a smoke
  transcript against a real store (see each spec).
- Commits end with `Co-Authored-By:` only — **no `Claude-Session:` lines**
  (history was scrubbed of them on 2026-09-28). Commit email is the GitHub
  noreply address (set repo-locally).

## Roles

Planner: Fable. Implementers: Sonnet 5.5 (confirm the `sonnet` alias
resolves to 5.5 before launch — on 2026-09-28 it still resolved to Sonnet 5).
Reviewers: Opus 5.5 (adversarial + correctness; security reviewer on #63).

## Data notes for the operator

- The current download list contains **40 rows from one linked discussion
  group** whose parent channel the list's own README says is excluded — a
  likely catalogue filter leak; fix it at the source. paperboy can't resolve
  those rows anyway (no stored username; see the #68 spec §4), so they would
  report `unresolvable`. (Never name real collection targets in this repo —
  it is public.)
