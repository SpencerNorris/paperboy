# Media storage batch — overview and run order

**Status:** draft for Gate A, 2026-09-28. **Operator goal:** pull ~146 GB of
catalogued media (1,620 posts, 11 channels, files up to 2.4 GB) into the
content-addressed archive and the GCS bucket, preferably from the OSINT VM
(2 vCPU, 7.7 GiB RAM, 38 GB free disk), falling back to the Mac — into a
`default` profile that first has an unrelated investigation split out.

This file is the index. Each feature has its own spec, written so it can be
handed to one implementing agent on its own.

| Spec | Issue | Run | Why it's needed |
|---|---|---|---|
| [`…-media-relative-paths-design.md`](2026-09-28-media-relative-paths-design.md) | #62 | overnight 1 | Portable media keys — prerequisite for the split, the VM and GCS |
| [`…-media-streaming-design.md`](2026-09-28-media-streaming-design.md) | #64 (+#53) | overnight 1 | 2.4 GB files must not sit in RAM; the disk must not fill silently |
| [`…-pacing-safety-factor-design.md`](2026-09-28-pacing-safety-factor-design.md) | #69 | overnight 1 | Conservative pacing; sleep through long waits instead of killing phases |
| [`…-profile-split-design.md`](2026-09-28-profile-split-design.md) | #70 | overnight 2 | Get the unrelated investigation out of `default` before adding 146 GB to it |
| [`…-media-list-fetch-design.md`](2026-09-28-media-list-fetch-design.md) | #68 | overnight 2 | The pull is driven by a cross-channel CSV |
| [`…-media-gcs-backend-design.md`](2026-09-28-media-gcs-backend-design.md) | #63 | daytime | VM disk < pull size; needs IAM, VM proxy and a VM smoke with the operator |

## Run order

- **Overnight run 1 — `federated-run`, 3 features, base `dev/media-storage`:**
  #62, #64, #69. Overlaps: #62/#64 share `collectors/media.py` (see Seams);
  #69 only adds `Settings` fields next to theirs (trivial merge).
- **Overnight run 2 — after run 1 is integrated on `dev/media-storage`:**
  #70 and #68. Both touch the replay side of `reproject` (#70: a target
  filter in the run loop; #68: a `ChannelContextReused` replay marker) —
  small, separate hunks; the integrator resolves. If the orchestration can't
  chain runs unattended, run them as two `single-feature-run`s back to back.
- **Daytime:** #63 (security reviewer on; ops prerequisites in its spec).
- **Then the operator procedure** in the #70 spec (split, verify, swap), a
  `fetch-media --dry-run` on the cleaned list, and the pull.
- **Mac fallback** is possible after runs 1–2: the data volume had 190 GiB
  free on 2026-09-28, so the full pull fits but leaves ~44 GiB; P1+P2
  (~90 GB) is comfortable. Set the #64 free-disk floor accordingly.

## Definition-of-done smokes that need the operator's Telegram account

#64, #69 and #68 (the live slice) smoke against live Telegram with the
collecting account. Unattended runs **must not** do this unless the operator
authorizes it at Gate A; otherwise implement, test, open the PR, and leave
the live smoke marked pending for the morning. #62, #70 and #68's `--dry-run`
smoke offline on a copy of the real store and can complete overnight.

## Seams between #62 and #64 (read before implementing either)

Both edit the write path in `collectors/media.py` (and the avatar twin in
`collectors/profiles.py`). To keep the parallel legs mergeable:

- **#62 owns naming and reading:** it introduces `media_key(sha256, ext) ->
  str` (`"media/ab/<sha><ext>"`) and `resolve_media_key(settings, profile,
  key) -> Path` in a new `src/paperboy/media_keys.py`, stores the key (not a
  path) in `media.path`, `custody_log.path` and new raw payloads, migrates old
  rows, and updates `replay.py`'s readers.
- **#64 owns writing:** it changes the gateway download signature to stream
  into a sink, adds the temp-file + atomic-rename writer and the disk guard.
  It keeps **today's** destination expression (`media_root / sha[:2] /
  f"{sha}{ext}"`) and today's `str(path)` stored value, untouched.
- **Integration:** replace #64's destination expression with
  `resolve_media_key(..., media_key(sha, ext))` and its stored value with the
  key. This is the one expected conflict; the integrator resolves it and
  re-runs the full suite.

## Shared constraints (from CLAUDE.md; non-negotiable)

- Read-only against Telegram; every RPC goes through `Budget` — no collector
  calls the gateway raw.
- Raw first: `raw_records` stays append-only. Old payloads are never
  rewritten; the projection normalizes them.
- Storage and guardrails are settled decisions: #62 lands ADR-0007, #63
  ADR-0008, #69 amends ADR-0003 — before code.
- Tests: `uv run pytest -q` (point `TMPDIR`/`--basetemp` at a volume with
  free space — a near-full root disk causes spurious SQLite I/O errors),
  `uv run ruff check`, `uv run pyright` — all green per task.
- Commits end with `Co-Authored-By:` only — **no `Claude-Session:` lines**
  (history was scrubbed of them on 2026-09-28). Commit email is the GitHub
  noreply address (set repo-locally).
- Never name real collection targets, their ids, or local machine paths in
  anything committed — this repo is public.
- Implementation worktrees start from `main`: these specs must be merged to
  `main` (or the dev branch cut from them) before launch.

## Roles

Planner: Fable. Implementers: Sonnet 5.5 (confirm the `sonnet` alias
resolves to 5.5 before launch — on 2026-09-28 it still resolved to Sonnet 5).
Reviewers: Opus 5.5 (adversarial + correctness; security reviewer on #63).

## Data notes for the operator

- The current download list contains **40 rows from one linked discussion
  group** belonging to the investigation being split out (#70). Drop them
  when the list is regenerated from the clean store; paperboy would report
  them `unresolvable` anyway.
