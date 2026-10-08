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

A file's full location is **store root + key**. A run picks one store: the
profile folder (the default), or a bucket (`--media-store gs://<bucket>/<prefix>`)
where the same key sits under the prefix. The database remembers which store
each file went to (`custody_log.store`: `local` or the bucket URL), because the
same fingerprint can be in one store and not yet in the other.

Detail: [`adr/0007-media-keys.md`](adr/0007-media-keys.md) (#62),
[`adr/0008-media-stores.md`](adr/0008-media-stores.md) (#63).

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

Large media pulls download once into the run's media store: the target
profile's `media/` by default. Downloads stream to disk in chunks, so a 2.4 GB
video never sits in memory, and a free-disk floor stops the run before the
disk fills (#64).

**A bucket run (#63).** With `--media-store gs://<bucket>/<prefix>`, each file
still streams into a short-lived temp file (we only know its fingerprint, and
so its final name, once it has fully arrived). Then paperboy asks the bucket
whether that name exists. If it does, the temp file is thrown away and only a
custody row is written; if not, the file is uploaded **create-only** (the
bucket refuses to replace an existing object) and its checksum is compared
with the one computed while streaming. The temp file is then deleted, so no
local copy remains. The bucket keeps deleted or replaced files for months, so
paperboy has no way to delete or overwrite anything in it, ever. A checksum
mismatch writes no rows and leaves the object for you to inspect. `reproject`
later reads those files back from the bucket (read-only) to re-check their
fingerprints, which is the one case where a rebuild touches the network; it
never contacts Telegram. "Already have it" is checked per store: a file
from a local run is downloaded again for a bucket run.

`paperboy fetch-from-list LIST` (#68, #91; called `fetch-media` before #91) pulls
the posts, and then their media, for a prioritised list of messages that can span
many channels. In plain terms:

1. **Read the list.** Each row names one message. If any row is malformed the
   whole command stops before touching anything and says which lines.
2. **Sort the rows offline.** Against what is already stored, a row is a
   duplicate, excluded (`--exclude-target` names its channel, and also its linked
   discussion group; excluding only a group never excludes its parent channel) or
   still to do. Every
   row that is still to do will have its post fetched, whether we hold it or
   not, so the offline step only counts what we already have: posts in the
   store, posts not yet collected, media already in this run's store. No network
   is used, so `--dry-run` can show this for the whole list, channel by channel,
   for free.
3. **Cut the to-do rows into segments.** One segment is "this priority, this
   channel", in the order they first appear in the list, so all the important
   rows are fetched before the rest. Inside a segment ids go in message-id
   order.
4. **Run each segment as ordinary collect runs** over the same session, in
   three steps (separate runs: the channel once, then the posts, then the media). First the channel, reached by its id the same way `collect` does
   (a key we already saved, a message that mentioned it, or a handle we can
   verify; no name lookup is needed when we hold a key; a link with a handle we
   have never seen is looked up by that handle). A channel we cannot reach is
   marked `no_access` for its rows and the command goes on. Only once the channel is
   established is `--exclude-target` applied to it, because a discussion group
   of an excluded channel is recognisable only from what Telegram says about it
   (its metadata is stored, nothing else is fetched). Then the **posts**:
   one request per 100 ids, and each answer is saved as raw first and then
   turned into a message row, exactly as `history` does it, so a post we never
   had appears, an edit becomes a revision, new view counts become a metric row
   and a deleted post becomes a tombstone. Posts we already hold are asked for
   again so they are current. Then, once the posts are stored, the **media**: for each fetched
   post we look at the photo or document it carries *now* (Telegram's id for it,
   remembered with every custody row, [ADR-0009](adr/0009-custody-records-content-key.md)) and download only what this run's
   store does not hold. A post edited to a different photo therefore downloads the
   new one. A repost of a file we hold, in the same channel, is not downloaded again but
   still gets its own custody row (where and when the file appeared); a repost
   whose file is held under *another* channel is just reported as already stored
   (no download, no custody row); a message that already has its row is left
   alone, so re-runs add nothing. Later segments of a channel reuse the first
   one's access and leave a small note, `ChannelContextReused` (channel id and
   the run that got access, never the key). A segment also notes which message
   ids its media step was allowed to fetch (`MediaSelection`), just before the
   download starts.
5. **Replay follows the notes.** `reproject` reads those notes, and the
   `channels.getMessages` tag on the saved post answers, to rebuild exactly the
   same messages, revisions, `media` and `custody_log` rows, offline. The one
   schema change is migration 0008, a `content_key` column on `custody_log`. It replays a step only where it actually ran: a channel that was
   refused leaves nothing to replay.
6. **A report for every row.** The report CSV has one line per input row, what
   happened to its post and what happened to its media. If the command stops
   early (a hard stop, the disk floor, a long Telegram wait) the rows it did not
   reach say `not_attempted`; run the same command again and it picks up where it
   left off, fetching the posts again and skipping the media we now hold
   (`already_stored`).

Details, outcomes and stop rules: [`features/fetch-from-list.md`](features/fetch-from-list.md).

## 7. Where to read next

| Question | Doc |
|---|---|
| What's in each table? | [`data-model.md`](data-model.md) |
| How do I run it safely? | [`opsec.md`](opsec.md), README "Safety & guardrails" |
| Why was X decided? | [`adr/`](adr/) |
| What does Telegram expose at all? | [`research/telegram-extraction-surface.md`](research/telegram-extraction-surface.md) |
| How was feature Y built and smoke-tested? | [`features/`](features/) |
| What's being built next? | `superpowers/specs/2026-09-28-media-storage-overview.md` |
