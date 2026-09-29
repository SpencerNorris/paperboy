# Split a mixed profile into per-investigation profiles via `reproject`

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #70.
**Order:** 4 of 5 in the sequential chain (after #69, #62, #64). **Depends
on:** #62 (profile-relative media keys — the split copies media by key into
another profile's folder) and #64 (replay streams media through the sink).
Read `2026-09-28-media-storage-overview.md` first, including the live smoke
protocol. **The agent never performs the swap (§3 step 4) on the real
profile** — that is the operator's call after review.
**ADR:** none new — this is the raw-first design (ADR-0002, ADR-0005) used
for its intended purpose; note the new flags in `docs/features/reproject.md`.

## 1. Problem

An agent collected a channel belonging to a separate investigation (plus its
linked discussion group) into the `default` profile, so one `paperboy.sqlite`
now mixes two investigations. On 2026-09-28 that is ~5,500 of 59,050
messages and ~450 media rows. The same mix has propagated to the bucket
snapshot and the VM's read-only copy. The operator wants the unrelated
channel out of `default` and kept in a profile of its own. (Never name real
collection targets in this repo — it is public; use `@<target>` below.)

## 2. Approach: reproject with a target filter

`raw_records` holds every Telegram response verbatim, grouped into runs
(ADR-0005), and each run is a collect against one resolved target.
`reproject` already rebuilds a store from them run by run, target by target
(`resolve_targets(run)`), offline. So a split is two filtered reprojections
of the same untouched source:

```
# 1. default without the unrelated target (media already in place)
paperboy reproject --profile default --exclude-target @<target> \
    --out data/default/paperboy.split.sqlite

# 2. the unrelated target alone, into its own profile (media copied there)
paperboy reproject --profile default --include-target @<target> --out-profile <name>
```

New `reproject` options:

- `--include-target T` / `--exclude-target T` (repeatable, mutually
  exclusive). `T` is `@username`, `username`, or a channel id. Matching is by
  **resolved channel id**, not string: map each run's raw target (which may
  be `@x` or `x` — both occur in real sources) to the channel id its
  `ResolvedPeer` record resolved to, and compare ids. A linked discussion
  group is collected inside its parent's run, so it follows its parent
  automatically — no separate flag.
- Unknown `T` (never resolved anywhere in the source) → error listing the
  targets the source does contain. Never a silent no-op.
- `--out-profile P`: write the output to `data/P/paperboy.sqlite` and make the
  collectors write media under `data/P/media/` (pass `P` as the `profile` to
  `collect_channel`; `ReplaySource.media_root` still reads bytes from the
  **source** profile). The replayed `media` phase therefore copies exactly
  the files the output references — content-addressed, so a file referenced
  by both investigations exists in both profiles. Refuse if
  `data/P/paperboy.sqlite` already exists. `--out` and `--out-profile` are
  mutually exclusive.
- Logging: one INFO line per run: `run_id, target, included|excluded`; a
  summary of included/excluded run counts.

**Why reproject rather than deleting rows from a copy.** Surgical deletion
has to chase every table a channel touches (peers seen only there, users,
edges, participants, custody, raw rows by context) and gets attribution
wrong for entities seen in both. Reproject rebuilds each output from exactly
the runs it should contain, raw rows included. The trade-off, to state
plainly in the feature doc: the outputs are reprojections, so they inherit
reproject's known residuals (#36 custody undercount across sessions, #37
backfill absorption, #38/#39 order-dependent peer lineage, #50 phantom
person-layer bookkeeping). The DoD measures them rather than assuming they
are zero.

## 3. Operator procedure (documented in `docs/features/reproject.md`)

1. Back up: `cp data/default/paperboy.sqlite data/default/paperboy.pre-split.sqlite`.
2. Run the two commands above.
3. Verify (§5 checks) — both outputs.
4. Swap: `data/default/paperboy.split.sqlite` → `data/default/paperboy.sqlite`
   (keep the pre-split backup until the bucket holds a verified clean
   snapshot).
5. List (don't delete) media files in `data/default/media/` no longer
   referenced by the new default store — provide
   `scripts/unreferenced_media.py --profile default` (read-only) that prints
   them with sizes; deletion is the operator's call.
6. Upload the clean snapshot to the bucket as usual; replace the VM copy.
   Note: objects already in the bucket stay under its retention policy.

## 4. Tests (write first, see them fail)

- Fixture source with two targets in separate runs (one with a linked
  group): `--exclude-target` output has no messages, edges, participants,
  media, custody or raw rows for the excluded channel or its linked group;
  `--include-target` output has only those.
- `@x`, `x` and the numeric id all select the same runs.
- Unknown target → error naming the available targets; no output written.
- `--out-profile` writes media under the new profile and the source media
  dir is unchanged (hash the source media dir before/after).
- Include + exclude of the same source partition it: every run is replayed
  in exactly one output (log assertion).
- Existing reproject tests unchanged when no filter is given.

## 5. Definition of done (offline smoke on a copy of the real store)

Offline (no Telegram), under the overview's smoke protocol: back up the real
store into the scratch data dir with `sqlite3 … ".backup …"`, symlink
`<scratch>/default/media` to the real media dir (read-only use — show its
file count and total bytes unchanged before/after), and run §3 steps 2–3
with `PAPERBOY_DATA_DIR=<scratch>`, so both outputs and the split-out
profile's copied media land in the scratch dir. Paste (redacted — the
excluded target is `@<target>`):

- Per-table row counts: source, clean default, split-out profile, and
  `source − split-out` next to `clean default`, with each difference
  explained (expected: shared entities such as users/peers seen in both, and
  the known residual issues above — cite which).
- Leak check on the clean default: zero `messages` whose `channel_id` is the
  excluded channel or its linked group; zero `raw_records` whose context
  names them; zero `media` rows whose `message_uri` is in them.
- Leak check on the split-out profile: zero messages from any other channel.
- `sha256` spot check of 5 copied media files in the new profile against
  their `media.sha256`.
- `paperboy status` for both profiles.

## 6. Out of scope

Removing objects from the bucket (retention), editing the VM copy, a general
"merge profiles" command.
