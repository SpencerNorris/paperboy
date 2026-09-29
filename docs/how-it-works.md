# How paperboy works, in plain language

Start here. It explains the moving parts and the words the other docs use
(raw records, projection, replay, reproject, profile, media key) without
assuming you've read the code. Each section ends with where to go for detail.

## 1. Two layers: the receipts and the spreadsheets

```
paperboy.sqlite
├── raw_records ........ THE RECEIPTS. Every answer Telegram ever gave us,
│                        stored exactly as it arrived. Append-only; never edited.
│                        This is the evidence.
└── messages, media, users, channels, edges, ...
                         THE SPREADSHEETS ("projections"). Tidy tables built
                         from the receipts so you can query them (Datasette,
                         sqlite3). Disposable: they can always be rebuilt.
```

Every collect writes the receipt first, then updates the spreadsheets. That is
the **raw-first** rule ([ADR-0002](adr/0002-storage.md)). If a spreadsheet is ever wrong, you don't
re-scrape Telegram; you rebuild it from the receipts.

Detail: [`data-model.md`](data-model.md) (every table and column).

## 2. Collect: talking to Telegram

`paperboy collect @channel` runs a series of **phases** (`channel`, `history`,
`media`, `discussion`, `graph`, `web`, `participants`, `profiles`). Each phase
is a **collector** that asks Telegram for something through one **gateway**,
and every request passes through the **budget**. The budget spaces requests
out (`--pacing-factor`), waits out Telegram's "slow down" replies (FLOOD_WAIT)
up to a ceiling (`--max-flood-sleep`), and stops the run on anything that
risks the account.

Detail: [`features/collect-channel.md`](features/collect-channel.md),
[`features/pacing.md`](features/pacing.md), [`opsec.md`](opsec.md).

## 3. Media: files live beside the database, found by fingerprint

Photos and videos are too big for the database. Each file is saved once under
its **fingerprint** (SHA-256), so the same picture posted in ten channels is
stored once. The database row holds a **media key**, a path relative to the
profile folder:

```
data/default/
├── paperboy.sqlite ......... media row: sha=abc…, path="media/ab/abc….mp4"
└── media/ab/abc….mp4 ....... the actual bytes
```

Because the key is relative to the profile folder, you can move or copy the
whole folder (to another disk, the VM, the bucket) and nothing breaks.

Detail: [`adr/0007-media-keys.md`](adr/0007-media-keys.md) (#62).

## 4. Replay and reproject: rebuilding the spreadsheets offline

`paperboy reproject` throws the spreadsheets away and rebuilds them from the
receipts, with no internet and no credentials. It does this by **replay**: it
runs the same collectors as a live collect, but plugs in a *fake Telegram*
(`RawReplayGateway`) that answers every question by looking up the matching
receipt. The collectors can't tell the difference, so a rebuild matches what a
live collect would have produced.

```
LIVE COLLECT                           REPLAY (reproject)
collector ──asks──► real Telegram      collector ──asks──► fake Telegram
                                                           (reads receipts)
        writes ──► paperboy.sqlite             writes ──► a NEW database (--out)
```

Media during replay: the receipt says "file abc… is at `media/ab/abc….mp4`".
Replay opens that existing file, checks its fingerprint matches, and writes a
row pointing at it. **It copies nothing and writes nothing into the source**,
only the new database.

Replay goes **run by run**: each past `collect` is one "run", replayed in order
([ADR-0005](adr/0005-run-structure.md)).

Detail: [`features/reproject.md`](features/reproject.md).

## 5. Profiles: one folder per investigation

A **profile** is one self-contained folder: `data/<profile>/paperboy.sqlite`
plus its `media/`. Each profile belongs to one investigation, so you can back
it up, move it, share it or delete it on its own. (Telegram credentials are
also per profile, in the OS keychain, never in the folder.)

**Splitting a mixed profile** (#70) is two filtered rebuilds of the same source:

```
data/default/ (mixed)
   │  reproject --exclude-target @<other>        reproject --include-target @<other>
   ▼                                              --out-profile <other>   ▼
data/default/ (clean)                         data/<other>/
├── paperboy.sqlite (rebuilt)                 ├── paperboy.sqlite (just that one)
└── media/  untouched; rows point here        └── media/  COPY of only that
                                                  investigation's files
```

Only the split-off investigation's files are copied, because the new profile
must stand alone. Once you delete the leftovers from `default`, that's a move,
not extra disk.

## 6. The media pull

Large media pulls (#68, `fetch-media --list`) download straight into the
target profile's `media/`, once. Downloads stream to disk in chunks, so a
2.4 GB video never sits in memory, and a free-disk floor stops the run before
the disk fills (#64).

## 7. Where to read next

| Question | Doc |
|---|---|
| What's in each table? | [`data-model.md`](data-model.md) |
| How do I run it safely? | [`opsec.md`](opsec.md), README "Safety & guardrails" |
| Why was X decided? | [`adr/`](adr/) |
| What does Telegram expose at all? | [`research/telegram-extraction-surface.md`](research/telegram-extraction-surface.md) |
| How was feature Y built and smoke-tested? | [`features/`](features/) |
| What's being built next? | `superpowers/specs/2026-09-28-media-storage-overview.md` |
