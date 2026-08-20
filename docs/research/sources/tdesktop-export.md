# Telegram Desktop "Export chat history" — internals (condensed)

Source: tdesktop `dev` @ `8e18cb71103d83d7d98994ff27f0a2bca55c489c` (2026-08-07),
`Telegram/SourceFiles/export/**`; https://core.telegram.org/import-export;
https://core.telegram.org/api/takeout; https://bugs.telegram.org/c/60.

## Verdict: a schema reference, not a collection path

- **Full account export structurally excludes other people's messages in public
  groups/channels.** `export_settings.cpp` forces `MustNotBeFull = PublicGroups |
  PublicChannels`; the UI force-checks "Only my messages". Mechanically it swaps
  `messages.getHistory` for `messages.search(from_id=inputPeerSelf)`. Official:
  "public group and channel exports will only contain messages sent by the user
  requesting the export."
- **Per-chat export** (`export_controller.cpp`) normalises to full history, one
  channel at a time, GUI-only. **No CLI flag exists** (full launcher flag table
  checked: `-debug -testagent -key -autostart -fixprevious -cleanup -noupdate
  -tosettings -startintray -quit -workdir -- -scale`).
- **Content-protected chats ("Restrict saving content") refuse export entirely**
  (`data_peer.cpp` `canExportChatHistory()` → false when `!allowsForwarding()`).
- **No resume** ("If you do, you'll need to start over."), no FLOOD_WAIT handling
  in export code, 2FA gate is dead code.
- **≈24 h takeout delay on a fresh session** (server-side `TAKEOUT_INIT_DELAY_%d`),
  or confirm from another device.
- Never collected: view counts, forward counts, reply/comment counts, participant
  or admin lists, deleted messages, edit history, `grouped_id` (albums),
  `forwardedDate` in JSON (HTML only).

## JSON format traps (parser-breaking, source-verified)

1. Almost every field is conditional; only `id`, `type`, `date`, `date_unixtime`
   are unconditional.
2. `date_unixtime` / `edited_unixtime` are **quoted strings**; `id`, `width`,
   `height`, `file_size`, `duration_seconds` are numbers (official doc is wrong
   on width/height).
3. `"from": null`, `"actor": null`, `"name": null` are real.
4. `date`/`edited` are local time without offset — use `*_unixtime`.
5. `text` is polymorphic (string or mixed array); parse `text_entities`.
6. Chat `id` is bare; `from_id`/`actor_id`/`forwarded_from_id`/`reply_to_peer_id`
   are prefixed (`user123`, `channel123`).
7. A plain document emits no `media_type`; `ActionCustomAction` has no `action`.
8. `rich_message` replaces `text`/`text_entities` when present.
9. Official schema page is stale: omits `reactions`, `forwarded_from_id`,
   `reply_to_peer_id`, `media_spoiler`, `inline_bot_buttons`, `*_file_size`,
   ~25 newer service actions (`boost_apply`, `send_star_gift`, …).
10. Reactions: `{type, count, emoji|document_id, recent:[{from, from_id, date}]}`
    — `recent` is a sample, not the full list.

## Ecosystem

- `mlomb/chat-analytics` (1.1k★, TS, AGPL) is the only widely used consumer.
- `flexagoon/ream` (Python, MIT, 2026-03) is a headless re-implementation with
  parity tests — mine those if we ever emit a tdesktop-compatible JSON view.
- No maintained, schema-complete parser library exists in any language.
