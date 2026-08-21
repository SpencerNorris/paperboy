# paperboy

A local, read-only command-line tool that collects everything obtainable about a
Telegram channel or supergroup into a single SQLite database — messages with
their edit and deletion history, media, comment threads, the people who are
discoverable and their public profiles, the forward/mention/similar-channel
graph, and web-archive snapshots — for open-source investigation and
journalism. Everything runs on your machine; nothing is uploaded.

**Status:** Phase 1 (core) shipped — channel metadata, full message history
with edit revisions and deletion tombstones, `pts`-based incremental sync,
JSONL export, and an opsec preflight (`paperboy doctor`) all work end to end
against live Telegram. Media, comment threads, people discovery, the graph,
and web-archive snapshots are Phase 2, not yet built. Start with
`docs/research/telegram-extraction-surface.md`,
`docs/superpowers/specs/2026-08-20-paperboy-design.md`, and
`docs/features/collect-channel.md`.

## Disclaimer

paperboy is an unofficial third-party client built on the Telegram API. It is
not affiliated with or endorsed by Telegram. It only reads data the logged-in
account is already permitted to see; it does not circumvent privacy settings.
Using it is subject to Telegram's Terms of Service, API Terms of Service and
Content Licensing Terms — in particular, data collected with it must not be
used to train, fine-tune or evaluate AI/ML systems. Automated use of a Telegram
account can result in that account being limited or banned. You are
responsible for ensuring your use is lawful in your jurisdiction and
proportionate to a legitimate purpose, and for handling any personal data you
collect accordingly.
