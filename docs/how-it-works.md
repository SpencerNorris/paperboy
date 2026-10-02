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

`paperboy collect @channel` (or `paperboy collect 123`, by channel id) runs a series of **phases** (`channel`, `history`,
`media`, `discussion`, `graph`, `web`, `participants`, `profiles`). Each phase
is a **collector** that asks Telegram for something through one **gateway**,
and every request passes through the **budget**. The budget spaces requests
out (`--pacing-factor`), waits out Telegram's "slow down" replies (FLOOD_WAIT)
up to a ceiling (`--max-flood-sleep`), and stops the run on anything that
risks the account.

A target is a handle or a channel id. Telegram needs an id *plus* a secret
"access hash" that it only gives an account when it shows it the channel, so
by id the first step finds one already on file, or a message that referenced
the channel, or a known handle that still points at the same id, and writes
down which it used (a `ChannelAccess` receipt). The account must already have
been shown the channel; paperboy never guesses a hash.

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
live collect would have produced. Replay only reads the original profile;
the rebuilt database and its log are written beside `--out`.

```
LIVE COLLECT                           REPLAY (reproject)
collector ──asks──► real Telegram      collector ──asks──► fake Telegram
                                                           (reads receipts)
        writes ──► paperboy.sqlite             writes ──► a NEW database (--out)
```

Media during replay: the receipt says "file abc… is at `media/ab/abc….mp4`".
Replay opens that existing file, checks its fingerprint matches the receipt (a
file that no longer hashes to it is skipped with a warning, never trusted), and
writes a row pointing at it. **It copies nothing and writes nothing into the
source**, only the new database. The one exception is `--out-profile`
(section 5): there the checked file is copied into the *new* profile.

Replay goes **run by run**: each past `collect` is one "run", replayed in order
([ADR-0005](adr/0005-run-structure.md)).
While replaying a run, the fake Telegram reads that run's receipts into an
in-memory index once and answers every question from it (instead of
re-scanning the run for each one), which is what lets a rebuild scale to a
real archive.

Detail: [`features/reproject.md`](features/reproject.md).

## 5. Profiles: one folder per investigation

A **profile** is one self-contained folder: `data/<profile>/paperboy.sqlite`
plus its `media/`. Each profile belongs to one investigation, so you can back
it up, move it, share it or delete it on its own. (Telegram credentials are
also per profile, in the OS keychain, never in the folder.)

**Splitting a mixed profile** (#70) is two filtered rebuilds of the same source:

```
data/default/ (mixed)
   │ reproject --exclude-target @<other>      reproject --include-target @<other>
   │           --out <file>                              --out-profile <other>
   ▼                                                       ▼
data/default/ (clean, after you swap the file in)     data/<other>/
├── paperboy.sqlite (rebuilt)                         ├── paperboy.sqlite (just that one)
└── media/  untouched; rows point here                ├── paperboy.sqlite.log
                                                      └── media/  COPY of only that
                                                          investigation's files
```

`@<other>` may be spelled `@name`, `name` or a channel id; it matches by the
channel it *resolved to*, and its linked discussion group comes along. An
unknown name is an error that lists what the source contains, before anything
is written. Only the split-off investigation's files are copied (into
`data/<other>/media/.incoming/`, then renamed into place, so a crash never
leaves a half file under a real name), because the new profile must stand
alone. The old profile's `media/` keeps the copies you no longer need:
`uv run python scripts/unreferenced_media.py --profile default` lists them
(it never deletes). Once you delete the leftovers, that's a move, not extra
disk. Steps and caveats: [`features/reproject.md`](features/reproject.md).

## 6. The media pull

Large media pulls download straight into the target profile's `media/`, once.
Downloads stream to disk in chunks, so a 2.4 GB video never sits in memory,
and a free-disk floor stops the run before the disk fills (#64).

`paperboy fetch-media LIST` (#68) pulls media for a prioritised list of
messages that can span many channels. In plain terms:

1. **Read the list.** Each row names one message. If any row is malformed the
   whole command stops before touching anything and says which lines.
2. **Sort the rows offline.** Against what is already stored, every row is
   labelled: already downloaded, not in the store, no media, deleted, a
   channel we cannot look up by name, or still to do. No network is used, so
   `--dry-run` can show this for the whole list for free.
3. **Cut the to-do rows into segments.** One segment is "this priority, this
   channel", in the order they first appear in the list, so all the important
   rows are fetched before the rest. Inside a segment files come in message-id
   order.
4. **Run each segment as an ordinary collect run** over the same session. A
   channel is looked up by name once per command (that call is one of
   Telegram's most rate-limited); later segments of the same channel reuse the
   result and leave a small note in the receipts, `ChannelContextReused`
   (channel id and the run that did the lookup, never the access hash). Each
   segment also notes which message ids it was allowed to fetch
   (`MediaSelection`).
5. **Replay follows the notes.** `reproject` reads those two notes to rebuild
   exactly the same `media` and `custody_log` rows, offline, with no new
   tables.
6. **A report for every row.** The report CSV has one line per input row and
   what happened to it. If the command stops early (a hard stop, the disk
   floor, a long Telegram wait) the rows it did not reach say
   `not_attempted`; run the same command again and it picks up where it left
   off, because finished rows now read `already_stored`.

Details, outcomes and stop rules: [`features/fetch-media.md`](features/fetch-media.md).

## 7. Where to read next

| Question | Doc |
|---|---|
| What's in each table? | [`data-model.md`](data-model.md) |
| How do I run it safely? | [`opsec.md`](opsec.md), README "Safety & guardrails" |
| Why was X decided? | [`adr/`](adr/) |
| What does Telegram expose at all? | [`research/telegram-extraction-surface.md`](research/telegram-extraction-surface.md) |
| How was feature Y built and smoke-tested? | [`features/`](features/) |
| What's being built next? | `superpowers/specs/2026-09-28-media-storage-overview.md` |
