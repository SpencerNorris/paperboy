# Telegram channel extraction surface — research synthesis

**Date:** 2026-08-20 · **Schema baseline:** TL layer 228 (some sub-reports at 223) ·
**Status:** research deliverable; nothing executed against a live account yet.
Raw, cited sub-reports live in `sources/` (see `sources/README.md`). Items
marked **UNVERIFIED** are collected in §7 as the first-spike smoke-test list.

## 0. The five facts that shape the design

1. **A broadcast channel's subscriber list is unobtainable.** `channels.getParticipants`
   → `CHAT_ADMIN_REQUIRED` for non-admins, any filter — even the admin list. Only
   `participants_count` is public. Person-data on a channel comes from the
   **linked discussion group** and message-adjacent vectors (§2.4).
2. **Supergroup enumeration is best-effort and declining.** Server caps
   `channelParticipantsRecent` far below the true total; "Hide members"
   (`channelFull.participants_hidden`, default threshold 100 members) returns
   **only admins + bots** to non-admins. The Telethon `aggressive` alphabet trick
   was neutered in 2022 (v1.25.1). Branch on `participants_hidden` /
   `can_view_participants` before sweeping.
3. **Privacy is enforced by omitting fields, not by errors.** `users.getFullUser`
   never says "denied". Model every optional field tri-state:
   present / absent-not-set / absent-privacy (disambiguators: `fallback_photo`,
   `by_me`, `private_forward_name`).
4. **`pts` is the sync primitive, not `last_message_id`.** Only
   `updates.getChannelDifference` (≈100 000-pts window) yields edits and
   **deletions** (`updateDeleteChannelMessages`) after the fact; views/forwards
   updates carry no `pts` and are snapshot-only. A channel can be watched
   **passively without joining** (public channels; cap 10 per session).
5. **Telegram's Content-Licensing terms prohibit access "for any purpose other
   than ordinary, legitimate, and intended use … as its user"**, with a carve-out
   for legitimate third-party clients, and flatly prohibit AI/ML training use.
   This tool sits in the gray zone every OSINT Telegram tool sits in; the
   realistic sanction is account limitation/termination, not legal action
   (Van Buren narrowed CFAA to access barriers, not ToS). See §5.

## 1. Access-level legend

| Tier | Meaning | Typical error |
|---|---|---|
| **A** anyone | Any logged-in account that can resolve the peer; **no join required** | — |
| **M** member | Must have joined (or channel is private) | `CHANNEL_PRIVATE` |
| **ADM** | Admin rights in the target | `CHAT_ADMIN_REQUIRED` |
| **PREM** | Telegram Premium on the collecting account | `PREMIUM_ACCOUNT_REQUIRED` |
| **SELF** | Only about your own account | — |
| **BLOCKED** | Withheld as anti-deanonymization | `BROADCAST_FORBIDDEN` |
| **WEB** | Plain HTTPS, no account | — |

## 2. What can be collected

### 2.1 Channel identity & metadata (A unless noted)

`contacts.resolveUsername` → `Channel` (+ `access_hash`, per-session and
non-transferable) → `channels.getFullChannel` → `channelFull`. Fields worth
persisting: `id, title, username, usernames[] (collectible flags), about, date
(creation), photo, participants_count, admins_count/kicked_count/banned_count
(ADM-populated), online_count, linked_chat_id, location, slowmode_seconds,
available_reactions, stats_dc, pts, ttl_period, level + boosts, emoji_status,
color/profile_color, wallpaper, stories, restriction_reason[]`, flags
`verified, scam, fake, gigagroup, forum, broadcast, megagroup, signatures,
signature_profiles, join_to_send, join_request, noforwards, participants_hidden,
can_view_participants, has_link, has_geo, has_scheduled, restricted, min`.
`channelFull` has a 60 s server cache TTL. Exported invite visible only to ADM.

### 2.2 Messages (A for public channels)

`messages.getHistory` (100/page, `InvokeWithoutUpdates` for the backfill
phase), `channels.getMessages` by id (≤200 ids; returns `messageEmpty` for
deleted/invisible ids; authoritative gap-filler), `messages.search` with 18
`MessagesFilter` variants + `messages.getSearchCounters`, `InputMessagesFilterPinned`.
Every `message` field: `id, date, edit_date, from_id, peer_id, post_author,
views, forwards, replies{replies, comments, recent_repliers[], channel_id, max_id},
reactions{results[], recent_reactions[]}, fwd_from{from_id, from_name, channel_post,
post_author, saved_from_peer/msg_id, date, imported, psa_type}, via_bot_id,
via_business_bot_id, reply_to{reply_to_msg_id, reply_to_peer_id, top_id,
quote_text/entities/offset}, media (21 `MessageMedia` kinds), entities (25
kinds incl. `text_url`, `mention_name`, `custom_emoji`), grouped_id (albums),
pinned, noforwards, silent, post, legacy, edit_hide, invert_media, offline,
from_boosts_applied, saved_peer_id, ttl_period, restriction_reason[],
quick_reply_shortcut_id, effect, factcheck, paid_message_stars, reply_markup`.
`messageService` + all 64 `MessageAction*` (join/leave/pin/title/photo/topic/
gift/boost/giveaway/…) — join events with timestamps live here.
`messages.getMessagesViews(increment=False)` refreshes views/forwards/replies
for ≤100 ids per call.
**Restricted channels** (`restriction_reason` porno/terms/sensitive): enforcement
is **client-side**; the API returns the content. Record the reason, keep the data.

### 2.3 Attachments & media

- Photos are server-re-encoded (`photo` has no mime/size/filename) — no EXIF.
  **Documents** (`messageMediaDocument`) carry `mime_type, size,
  documentAttributeFilename/Video/Audio(performer,title)/Sticker` and are
  byte-exact (except server-transcoded `alt_documents` for big-channel video):
  run EXIF/metadata extraction on documents. Record which kind each item was.
- `file_reference` expires — refetch the message before downloading. Downloads
  go to media DCs where parallel sessions are **allowed**; the main DC permits
  one MTProto session per auth key (`AUTH_KEY_DUPLICATED` kills it).
- Polls: question/answers/totals (A); **voters** `messages.getPollVotes`:
  groups only, non-anonymous polls only, **and you must vote first**
  (`POLL_VOTE_REQUIRED`) — leaves a footprint; gate behind a flag or exclude.
- Webpage previews (`messageMediaWebPage` + `messages.getWebPage`), geo/venue,
  contacts (vCard), giveaways (co-sponsor `channels[]`, `countries_iso2`) and
  `messageMediaGiveawayResults.winners[]` (≤100 user ids, in a public message).
- Sponsored messages: `messages.getSponsoredMessages(peer)` (A; the old
  `channels.getSponsoredMessages` is removed) → `sponsor_info` (legal
  advertiser disclosure), `url`, `title`. API ToS §3.3 asks channel-reading
  clients to support sponsored messages — collecting them is both compliant
  and useful.

### 2.4 People — who is actually reachable

| Vector | Access | Yield / caveat |
|---|---|---|
| Message authors (`from_id`) | A (public) / M | Highest yield in groups; arrive as **`min` users** — store `(chat, msg_id)` provenance and use `inputUserFromMessage` to fetch profiles |
| **Linked discussion group**: `messages.getDiscussionMessage` → `messages.getReplies`, or bulk `getHistory` on the group bucketed by `reply_to_top_id` | A unless `join_to_send` | *The* person vector for a broadcast channel; reading never requires joining |
| `channels.getParticipants(channelParticipantsMentions, top_msg_id)` | M | Officially returns **non-participant commenters** — sanctioned, not a hack |
| `messageReplies.recent_repliers` | A | Free with every post; poll over time |
| Reactors `messages.getMessageReactionsList` → `{peer_id, date, reaction}` | M, **groups only**; BLOCKED on channels | Exact reaction timestamps in supergroups |
| Join/leave service messages | A | Longitudinal membership incl. leavers; often hidden by admins |
| `messages.getMessageReadParticipants` | M | Groups ≤100 members, messages <7 days — lurkers |
| Forward attributions, `mention_name` entities | A | Cheap side-channel |
| `channels.getParticipants` Recent/Search/Admins/Bots | M (supergroups) | `channelParticipant.date` = join date for every enumerable member; `rank` (custom title) often leaks roles |
| `channels.getParticipant(user)` | M | **Per-user membership oracle** with join date — works where bulk enumeration doesn't |
| Basic groups `messages.getFullChat` | M | ≤200 members, with `inviter_id` + join date |
| `messages.checkChatInvite(hash)` | A | Private invite link preview **without joining**: title, photo, `participants_count`, sample `participants[]` |
| Boosters, invite importers, admin log, kicked/banned, recent requesters | **ADM** | Detect-and-skip for non-admins |

### 2.5 Per-user profile (`users.getUsers` batched triage → `users.getFullUser` per target)

`user`: `id, access_hash, first/last_name, username, usernames[]{editable,
active}, phone (privacy; may be set-but-empty), photo{has_video, personal,
stripped_thumb}, status (UserStatus* — recently/last_week/last_month/exact/empty;
`by_me` flag when *your* privacy hides it), bot + bot_info, verified, restricted
+ restriction_reason[], scam, fake, premium, support, emoji_status (incl.
`emojiStatusCollectible{slug}`), color, profile_color, stories_max_id,
stories_unavailable, contact_require_premium, bot_business, bot_has_main_app,
unofficial_security_risk`. `lang_code` is **bot-only — permanently absent**.
`userFull`: `about (bio), birthday (contacts by default), personal_channel_id +
personal_channel_message, business_work_hours{timezone_id}, business_location
{geo_point, address}, business_greeting/away/intro, common_chats_count (+
`messages.getCommonChats`), stories, stories_pinned_available, wallpaper,
stargifts_count (displayed), disallowed_gifts, bot_group/broadcast_admin_rights,
private_forward_name, settings (PeerSettings), blocked, phone_calls_available,
voice_messages_forbidden, translations_disabled, read_dates_private,
profile_photo / fallback_photo / personal_photo` (**`personal_photo` is set by
YOU — never ingest as target data; `fallback_photo` present + `profile_photo`
absent = proof you're privacy-excluded**).
`photos.getUserPhotos` → dated profile-photo history (the one official
retrospective identity signal). `messages.getPeerSettings` → `registration_month,
phone_country, name_change_date, photo_change_date` **only for a stranger who
messaged you first** — passive capture only, not queryable.
Gifts / Fragment / stories: see `sources/gifts-fragment-stories-namehistory.md`
— displayed gifts with dates and senders (A), collectible gift → **owner TON
address + NFT address + original sender/recipient/date** (A), pinned stories
with `mediaAreaGeoPoint{lat, long, street address}` (A), `stories.searchPosts`
by hashtag or geo area (A).
**Not obtainable:** username/display-name history (no API; only self-observed
`updateUserName` diffs or untrustworthy third-party bots), phone numbers beyond
privacy rules, exact registration date (ID-range interpolation only, emit an
interval), `lang_code`.

### 2.6 Discovery graph (A unless noted)

- `channels.getChannelRecommendations(channel)` — similar channels by
  subscriber-base overlap; 10 results (100 with Premium) but
  `chatsSlice.count` reveals the true degree.
- Forward graph from `fwd_from` (who this channel forwards *from*); mention
  graph from `mention`/`text_url` entities (t.me links); invite links in
  messages → `checkChatInvite` previews; `linked_chat_id`; giveaway
  co-sponsors; `via_bot_id`.
- Inverse ("who forwards *this* channel"): `stats.getMessagePublicForwards` is
  **ADM**; `channels.searchPosts` (global post search by text/hashtag) is
  **PREM** (+ Stars quota via `checkSearchPostsFlood`); `stories.searchPosts` is
  free. Third-party indexers (tgstat/telemetr) are the non-admin substitute.
- Forum topics `channels.getForumTopics` (title, icon, creator, date, counts).

### 2.7 Temporal / live (requires a persistent update loop)

| Signal | Historical? | Live | Notes |
|---|---|---|---|
| New messages | yes | `updateNewChannelMessage` (pts) | |
| Edits | **post-edit body only**, `edit_date` | `updateEditChannelMessage` (pts) | Snapshot every observed version |
| **Deletions** | only inside the pts window via `getChannelDifference`; otherwise id-gap + `messageEmpty` (ambiguous) | `updateDeleteChannelMessages` (pts) | Only unambiguous evidence |
| Views / forwards / reactions / replies counts | snapshot only | no pts — never replayable | Time-series table; refresh via `getMessagesViews` |
| Subscriber / online count | snapshot only | — | Poll `getFullChannel` |
| Pins, topics | yes | pts | |
| Join/leave stream | — | `updateChannelParticipant` is **qts = bots/admins only** | Users: service messages or admin log |
| Typing in discussion group | — | `updateChannelUserTyping` (`from_id`) | Ephemeral, deanonymizing |
| Stories | pinned only | 24 h window | |
| Edit history pre-edit bodies | **ADM only** (admin log, 48 h) | | |

### 2.8 No-account web vectors (WEB)

- `t.me/<name>`: og:title/description/image, subscriber count.
- `t.me/s/<name>`: server-rendered feed (20 posts/page, `?before=<id>` /
  `?after=<id>` pagination), per post: `data-post` id, exact ISO `datetime`,
  author signature, forwarded-from, reply-to, text with entities, media thumbs,
  polls, link previews, **abbreviated** views ("1.42M"), channel counters
  (subscribers, photos, videos, links). Plain nginx, no anti-bot challenge
  observed; `stel_ssid` cookie only. Not available for private channels or
  channels with previews disabled/restricted.
- `t.me/<name>/<id>?embed=1` single-post widget; `t.me/+<hash>` invite preview.
- **Wayback Machine CDX** (`web.archive.org/cdx/search/cdx?url=t.me/s/<name>*`)
  — verified working (snapshots of `t.me/s/telegram` from 2019): recovers
  **deleted posts, old descriptions, old subscriber counts, old usernames**.
  Rate-limit politely (the agent's burst of 14 requests was 429'd).
- `t.me/nft/<slug>` collectible-gift pages; `fragment.com/username/<n>` auction
  history with bidder wallets.
- Third-party indexers (tgstat.com, telemetr.io, lyzem, telegago): subscriber
  history, mentions/reposts, similar channels — paid/ToS-bound, **not
  researched in depth** (agent lost to the quota); v2 candidate.

### 2.9 Not viable as collection paths

- **Telegram Desktop export**: full export structurally excludes other people's
  public-channel messages; per-chat export is GUI-only, no resume, refuses
  content-protected chats, ≈24 h takeout delay (`sources/tdesktop-export.md`).
  Useful only as a JSON-schema reference.
- **Bot API**: cannot read history or enumerate members; only live observation
  if you are admin and add a bot.
- **Mobile/desktop clients**: everything they show maps to TL methods above.

## 3. Hard walls (do not promise these)

Broadcast subscriber/admin lists · reactors and poll voters on channels
(`BROADCAST_FORBIDDEN`) · username/name history · pre-edit message bodies ·
views/forwards history before first observation · deletions older than the pts
window (beyond ambiguous gaps) · boosters, invite importers, admin log, stats
(ADM) · `lang_code` · phone numbers outside privacy rules · exact account age.

## 4. Prior art — the gap

Nearly every open-source tool (Telepathy, tosint, telegram-tracker, TeleTracker,
tg-archive, telegram-scraper, tdl) is a *messages + media* dumper keyed on
`last_message_id`. Almost none persist raw TL, version edits, tombstone
deletions, snapshot counters, walk discussion threads, store `min`-peer
provenance, or collect the modern surface (recommendations, boosts, gifts,
stories, profile-photo history, sponsored messages). No single tool does all of
it. Full survey: `sources/prior-art.md`.

## 5. Legal, ToS and ethics — constraints to encode

**Telegram documents** (quoted in full in `sources/` and the scratch captures):
- ToS: "agree not to … send spam or scam users"; "Telegram additionally prohibits
  data scraping as part of its Content Licensing and AI Scraping Terms."
- Content Licensing Terms: access to user-generated content "for any purpose
  other than ordinary, legitimate, and intended use … as its user is prohibited";
  limited exception for "a legitimate third-party Telegram Client … in full
  compliance with the Telegram Terms of Service"; **firm prohibition on AI/ML
  training, fine-tuning, benchmarking, or deployment use**.
- API ToS: own `api_id` (§2.1); no actions without user consent (§1.4); no AI
  use (§1.5); channel-reading apps must support sponsored messages (§3.3);
  unofficial clients "are automatically placed under observation"; flooding ⇒
  permanent ban.
- Privacy Policy §3.1: screen name, profile photos and username "are always
  public" — the basis for collecting them; public-at-each-instant ≠ consent to
  longitudinal retention.

**Posture.** A read-only, rate-respecting client that reads public content with
the user's own account is the same activity Telethon/TDLib exist for and that
Bellingcat-class research routinely performs; the binding risk is ToS-based
account limitation, not criminal exposure (CFAA post-*Van Buren*; *hiQ v.
LinkedIn* for public data). GDPR applies if EU persons' data is stored for
non-household purposes: minimise, secure at rest, retain with a purpose, support
per-subject deletion.

**Guardrails (MUST unless stated):**
- Own `api_id`/`api_hash`; own, aged, non-purchased account; single session per
  auth key; never parallel sessions to evade limits.
- Honour `FLOOD_WAIT` per method; treat `PEER_FLOOD` / `FROZEN_METHOD_INVALID`
  as hard stops pointing at @SpamBot; conservative default pacing; daily budget.
- Read-only: never send, react, vote, join-request, or `suggestBirthday`
  (notifies the target). Joining a **public** channel/group is allowed but
  optional (passive mode preferred); joining via invite link to a private
  group requires the operator to assert authorisation (`--i-am-authorized`).
- Flag-gated (off by default): `getPollVotes` (requires voting), supergroup
  member enumeration on sessions younger than N days, third-party OSINT bots.
- **EXCLUDE**: `contacts.importContacts` / `resolvePhone` phone enumeration,
  `contacts.getLocated`, any add-member/invite capability, AI-training export.
- Store `restriction_reason` but do not download media flagged `porno`/illegal
  by default; document CSAM obligations (do not download; report).
- Encrypt-at-rest option for the SQLite store; README disclaimer; no
  `lang_code`/phone speculation; tri-state privacy fields, never "no photo".

## 6. Library & stack landscape (2026-08)

- **Telethon 1.44.0** (PyPI 2026-06-15; dev moved to codeberg.org/Lonami/Telethon,
  GitHub repo archived — *not* abandoned; layer 228 on the v1 branch). Full raw
  TL access (`telethon.tl.functions.*`), per-method `flood_sleep_threshold`,
  `*FromMessage` support, `to_dict()` on every TL object. **Recommended.**
  Telethon v2 is an unreleased Rust-backed alpha — not a foundation today.
- Kurigram (Pyrogram fork, layer 228, LGPL) — strong second, richer high-level
  API, single maintainer. Upstream Pyrogram is dead (layer 158). GramJS
  archived. TDLib is the most *correct* but hides raw TL and is a C++ dep.
- Local env: Python 3.14.3, uv 0.9.2, gh authenticated, sibling projects use
  `pyproject` + `uv`.

## 7. UNVERIFIED — first-spike smoke-test list

Spike 1 (2026-08-20, `scripts/spike.py` against `@telegram`, a broadcast
channel) settled items 1, 5, 7 and the recommendation-degree leak. The rest
need different target types (a visible supergroup, a restricted non-contact, a
stranger's collectible) and are settled by later spikes or in the DoD smoke.

1. ~~Telethon wheel layer~~ — **SETTLED: layer 227**; all core raw methods present.
2. `photos.getUserPhotos` on a privacy-restricted non-contact: empty vs partial. *(needs a restricted user target)*
3. `stories.getPeerStories` / `storyItem.views` as a non-contact. *(needs a user with active stories)*
4. `fragment.getCollectibleInfo` on a stranger's collectible. *(needs a collectible-holding target)*
5. ~~`channels.getParticipants(Admins)` on a broadcast channel as subscriber~~ — **SETTLED: `CHAT_ADMIN_REQUIRED`**, as expected. Even the admin list is walled.
6. Real `Recent` yield on a mid-size supergroup with members visible. *(needs a supergroup target)*
7. ~~`messages.getHistory` on a public channel without joining~~ — **SETTLED: works un-joined** (resolve + getFullChannel + getHistory all succeed; @telegram: 9.78M subs, pts=736, hidden=False).
8. `updates.getChannelDifference` passive mode without joining: delete events arrive? *(needs a watch window)*
9. `channelParticipantsMentions` returning non-participant commenters. *(needs a channel with a linked discussion group)*
10. FLOOD_WAIT onset for sequential `getFullUser` at 1 req/s on an aged account. *(needs a profiles run)*

**Bonus (settled):** `channels.getChannelRecommendations` returns the true
degree in `count` (87 for @telegram) while capping the returned list at 10 for a
non-Premium account — the true similar-channel count leaks regardless of
Premium.
