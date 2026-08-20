# Research sources (raw agent output — read with care)

These files are the **unedited reports of research sub-agents** run on 2026-08-20
against primary sources (core.telegram.org schema/method pages, TDLib and
Telegram Desktop source, Telethon docs/issues, GitHub). They are kept because
they carry the citations. They are **not** reviewed line-by-line; items the
agents could not establish are explicitly marked `UNVERIFIED` and are collected
in `../telegram-extraction-surface.md` §7 as the smoke-test list.

| File | Scope | Schema baseline |
|---|---|---|
| `mtproto-channel-messages.md` | Channel metadata, messages, media, comments, boosts, stories, admin-only surfaces, discovery, live updates / deletion detection | layer 228 |
| `mtproto-participants-users.md` | `channels.getParticipants` rules, hidden members, member discovery when hidden, `user`/`userFull` field-by-field, privacy gating, account-age estimation, `peerSettings` | layer 223 |
| `prior-art.md` | Survey of open-source Telegram OSINT/archiver tools; architecture lessons; 2026 library landscape | layer 228 |
| `gifts-fragment-stories-namehistory.md` | Star gifts → TON addresses, Fragment collectibles, stories + media areas, username/name history (negative result) | layer 223 |
| `tdesktop-export.md` | Telegram Desktop "Export chat history" internals — why it is a schema reference, not a collection path | tdesktop @ 8e18cb7 (2026-08-07) |

Nothing in these files was executed against a live Telegram account. "The
server returns X" statements are inferences from docs + official client source
unless a cited issue reports it empirically. The curated, deduplicated synthesis
is `../telegram-extraction-surface.md`.
