# MTProto Channel OSINT Inventory — Channel Metadata, Messages, and Message Attachments

Research vector: everything obtainable about a Telegram **broadcast channel** or **supergroup/megagroup**
via the official **MTProto user-account API** (the surface used by Telethon / TDLib).
Participants/per-user profile data is **out of scope** (separate research vector).

**Schema baseline: Layer 223** — `https://core.telegram.org/schema` renders `Layer 223`; `errors.json`
reports layer 227. The full TL schema was downloaded locally and grepped, so every constructor
signature below is verbatim unless flagged otherwise. Where the prose pages describe a newer layer
than the schema dump, that conflict is called out explicitly.

---

## 0. Access-level legend and structural constraints

Telegram documents access control **only** in the "Possible errors" table of each method page. That
table is the ground truth used throughout. ⚠ **Those tables are demonstrably incomplete** — several
methods whose prose says "admins only" list no `CHAT_ADMIN_REQUIRED` row. Such cases are flagged.

| Label | Meaning | Typical error |
|---|---|---|
| **A** (anyone) | Any logged-in account that can resolve the channel. **No join required.** | — |
| **M** (member) | Requires `channels.joinChannel`, or the channel is private. | `406 CHANNEL_PRIVATE` — *"You haven't joined this channel/supergroup."* |
| **ADM** | Requires admin rights in the target channel. | `400/403 CHAT_ADMIN_REQUIRED` |
| **SELF** | Only about your own account/peer. | varies |
| **PREM** | Requires a Telegram Premium subscription. | `403 PREMIUM_ACCOUNT_REQUIRED` |
| **BLOCKED** | Deliberately withheld as an anti-deanonymization measure. | `403 BROADCAST_FORBIDDEN` |

Four structural facts that colour everything below:

1. **`access_hash` is per-login-session and is explicitly an anti-scraping measure.**
   *"Access hashes may not be reused across different accounts or different login/auth sessions of the
   same account; however, they can be reused across different MTProto sessions linked to the same
   login/auth session. This is a core spam prevention feature of Telegram."*
   — <https://core.telegram.org/api/peers>. A harvested hash is worthless to another account.
2. **`min` constructors cripple the access hash.** *"In some situations [user] and [channel]
   constructors have reduced set of fields present (although `id` is always there) and `min` flag set."*
   The hash priority is **Full > Min > From-message > Zero**, and verbatim: a **min access hash**
   *"can only be used to fetch profile pictures using `inputPeerPhotoFileLocation`."* Everything else
   requires `inputChannelFromMessage#5b934f9d` / `inputUserFromMessage#1da448e2` — so **store
   `(peer_seen_in, msg_id, target_id)` for every min peer at ingest time**.
   — <https://core.telegram.org/api/min>, <https://core.telegram.org/api/peers>
3. **`channelFull` has a 60-second cache TTL.** *"Invalidate only `userFull` and `channelFull` entries
   60 seconds after they are stored."* — <https://core.telegram.org/api/peers>
4. **`AUTH_KEY_DUPLICATED` (406) permanently kills a session.** The main connection to a non-media DC
   normally permits only a single MTProto session (`config.tmp_sessions`); exceeding it means *"the
   session was already invalidated by the server and the user must generate a new auth key and login
   again."* **Media-DC file-transfer sessions are explicitly exempt** — parallelise downloads there,
   never on the main connection. — <https://core.telegram.org/api/errors>

---

## 1. Channel-level metadata

### 1.1 Resolution & identity

| Data item | Method / constructor | Access | Caveats | Source |
|---|---|---|---|---|
| Resolve `@username` → channel + access_hash | `contacts.resolveUsername#725afbbc username:string referer:flags.0?string` → `contacts.resolvedPeer#7f077ad9 peer:Peer chats:Vector<Chat> users:Vector<User>` | **A** | Errors: `USERNAME_INVALID`, `USERNAME_NOT_OCCUPIED`, `STARREF_EXPIRED`, `CONNECTION_LAYER_INVALID`. Users **and** bots. Entry point for any public channel. | <https://core.telegram.org/method/contacts.resolveUsername> |
| Resolve phone → user | `contacts.resolvePhone#8af94344 phone:string` | **A** | Only if the target's privacy allows. `PHONE_NOT_OCCUPIED`. Docs mandate ≤1 call / 3 s client-side. | <https://core.telegram.org/method/contacts.resolvePhone> |
| Username substring search | `contacts.search#11f812d8 q:string limit:int` → `contacts.found#b3134d9d my_results:Vector<Peer> results:Vector<Peer> chats users` | **A** | **Excludes your own contacts.** `QUERY_TOO_SHORT`, `SEARCH_QUERY_EMPTY`. | <https://core.telegram.org/method/contacts.search> |
| Upgrade `min` peers | `channels.getChannels#a7f6bbb`, `users.getUsers#d91a548`, `messages.getChats#49e9528f` | **A** (needs a usable hash) | All *"requiring the previously cached `access_hash`"* — pass `input*FromMessage` if all you have is min. | <https://core.telegram.org/api/peers> |
| Dialog state | `messages.getPeerDialogs#e470bcfd peers:Vector<InputDialogPeer>` → `messages.PeerDialogs` | **M**, users only | Unread counts, pts, notify settings. | schema L223 |
| Join | `channels.joinChannel#24b524c5 channel:InputChannel` | A → M | `CHANNELS_TOO_MUCH`, `USER_BANNED_IN_CHANNEL`, `INVITE_REQUEST_SENT`, `CHANNEL_PRIVATE`. Caps: `channels_limit_default`=500 / `_premium`=1000. | <https://core.telegram.org/method/channels.joinChannel> |

### 1.2 `Channel` object (arrives in every `chats:Vector<Chat>`)

```
channel#fe685355 flags:# creator:flags.0?true left:flags.2?true broadcast:flags.5?true
  verified:flags.7?true megagroup:flags.8?true restricted:flags.9?true signatures:flags.11?true
  min:flags.12?true scam:flags.19?true has_link:flags.20?true has_geo:flags.21?true
  slowmode_enabled:flags.22?true call_active:flags.23?true call_not_empty:flags.24?true
  fake:flags.25?true gigagroup:flags.26?true noforwards:flags.27?true join_to_send:flags.28?true
  join_request:flags.29?true forum:flags.30?true flags2:# stories_hidden:flags2.1?true
  stories_hidden_min:flags2.2?true stories_unavailable:flags2.3?true
  signature_profiles:flags2.12?true autotranslation:flags2.15?true
  broadcast_messages_allowed:flags2.16?true monoforum:flags2.17?true forum_tabs:flags2.19?true
  id:long access_hash:flags.13?long title:string username:flags.6?string photo:ChatPhoto date:int
  restriction_reason:flags.9?Vector<RestrictionReason> admin_rights:flags.14?ChatAdminRights
  banned_rights:flags.15?ChatBannedRights default_banned_rights:flags.18?ChatBannedRights
  participants_count:flags.17?int usernames:flags2.0?Vector<Username>
  stories_max_id:flags2.4?RecentStory color:flags2.7?PeerColor profile_color:flags2.8?PeerColor
  emoji_status:flags2.9?EmojiStatus level:flags2.10?int subscription_until_date:flags2.11?int
  bot_verification_icon:flags2.13?long send_paid_messages_stars:flags2.14?long
  linked_monoforum_id:flags2.18?long = Chat;
```

| Field | Meaning / OSINT value | Access |
|---|---|---|
| `id`, `access_hash` | Peer id + per-account token. IDs overlap across users/chats/channels — store the type. | A |
| `title` | Channel name | A |
| `username` | Main active username; absent on private channels | A |
| `usernames:Vector<Username>` — `username#b4073647 flags:# editable:flags.0?true active:flags.1?true username:string` | **All** usernames. `editable=false` ⇒ **bought on Fragment** (collectible/NFT) — a wealth/ownership signal traceable to a Fragment purchase record. Inactive entries are historical aliases still reserved. | A |
| `date` | *"Date user joined or channel creation date."* For a channel you have **not** joined this is the **creation date** — a direct age signal. Joining overwrites it with your join date. **Capture before joining.** | A |
| `photo:ChatPhoto` | Avatar (small/big ids, `dc_id`, `stripped_thumb`, `has_video`) | A |
| `broadcast` / `megagroup` / `gigagroup` | Type. *"Technically, supergroups are actually channels: they are represented by channel constructors, with the `megagroup` flag set to true."* Gigagroups: *"only admins will be able to write"*; conversion via `channels.convertToGigagroup` is one-way. | A |
| `forum`, `forum_tabs` | Topics enabled ⇒ `channels.getForumTopics` works | A |
| `monoforum`, `broadcast_messages_allowed`, `linked_monoforum_id` | "Direct messages to channel" surface | A |
| `verified` | Telegram-verified | A |
| `scam` / `fake` | *"probably a scam"* / *"reported by many users as fake or scam"* — Telegram's own abuse labelling | A |
| `restricted` + `restriction_reason` — `restrictionReason#d072acb4 platform:string reason:string text:string` | `platform` ∈ `ios`,`android`,`wp`,`all`, dash-concatenated (`android-ios`). `reason` ∈ `porno`, `terms`, `sensitive`. **Client-side enforcement only — see §9.** | A |
| `signatures`, `signature_profiles` | Post signatures on; `signature_profiles` ⇒ **the signature links to the admin's real profile** — a deanonymization vector. | A |
| `noforwards` | Content protection. Blocks `messages.forwardMessages`; **does not block API reads.** | A |
| `join_to_send`, `join_request` | Must join to write / approval needed. `join_request` ⇒ `channelFull.recent_requesters` populated (ADM). | A |
| `has_link` / `has_geo` / `slowmode_enabled` | Pair with `linked_chat_id` / `location` / `slowmode_seconds` | A |
| `call_active`, `call_not_empty` | **Live signal** — a voice chat/livestream is running *right now*, and whether anyone is in it | A |
| `participants_count` | Subscriber count | A |
| `level` | **Boost level**, 0–`boosts_channel_level_max` (=100). Free proxy for paid promotion. **No `premium.getBoostsStatus` call needed.** | A |
| `color`, `profile_color` — `peerColor#b54b5acf flags:# color:flags.0?int background_emoji_id:flags.1?long` | Palettes are boost-gated (`channel_min_level`) ⇒ a rare colour implies a high level. Decode via `help.getPeerColors` / `help.getPeerProfileColors`. | A |
| `emoji_status` — `emojiStatus`, or `emojiStatusCollectible collectible_id:long document_id:long title:string slug:string pattern_document_id:long center_color:int edge_color:int pattern_color:int text_color:int until:flags.0?int` | Collectible statuses are **NFT gifts**; `slug` is globally unique and traceable on Fragment. | A |
| `subscription_until_date`, `send_paid_messages_stars` | Paid-channel / paid-message monetization | A |
| `autotranslation` | Requires boost level ≥ `channel_autotranslation_level_min` (=3) | A |
| `stories_max_id` — `recentStory#711d692d live:flags.0?true max_id:flags.1?int`; `stories_hidden`, `stories_unavailable` | Cheap "does this channel post stories" probe | A |
| `bot_verification_icon` | Third-party bot verification badge | A |
| `admin_rights` / `banned_rights` / `default_banned_rights` | Your rights; **`default_banned_rights` reveals the supergroup's moderation posture** (may members send media, embed links, pin…) | A (defaults) / SELF |
| `min` | Reduced object — see §0.2 | — |
| `creator`, `left` | About *you* | SELF |

**min-mergeable fields** (the *only* fields that may be applied over a cached non-min channel when
`min` is set): title, megagroup, color, photo, username, usernames, has_geo, noforwards, emoji_status,
has_link, slow_mode_enabled, scam, fake, gigagroup, forum, level, restricted, restriction_reason,
join_to_send, join_request, is_verified, default_banned_rights, signature_profiles, autotranslation,
broadcast_messages_allowed, monoforum, forum_tabs, linked_monoforum_id, send_paid_messages_stars,
bot_verification_icon. — <https://core.telegram.org/constructor/channel>

### 1.3 `channels.getFullChannel` → `channelFull`

`channels.getFullChannel#08736a09 channel:InputChannel = messages.ChatFull`
→ `messages.chatFull#e5d7d19c full_chat:ChatFull chats:Vector<Chat> users:Vector<User>`

**Access: A** for a public channel. Errors are only `400 CHANNEL_INVALID`, `406 CHANNEL_PRIVATE`,
`403 CHANNEL_PUBLIC_GROUP_NA`, `400 CHAT_NOT_MODIFIED`, `400 MSG_ID_INVALID` — **no
`CHAT_ADMIN_REQUIRED`**, so everything below is readable by any account that can resolve the peer.
The `chats`/`users` vectors also hand you the **linked discussion supergroup object** (with its
access_hash) and the **bot accounts** in the channel, free.

| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `about` | string | Description/bio | A | Usually the richest single field — contact emails, external links, owner handles. |
| `participants_count` | flags.0?int | Subscribers | A | |
| `admins_count` | flags.1?int | Admin count | A | Visible to non-admins — reveals org size. |
| `kicked_count` / `banned_count` | flags.2?int | Moderation-intensity signal | A* | Both on flags.2. See §14. |
| `online_count` | flags.13?int | **Users online now** | A | **Live-only.** Poll over time to profile audience timezone. |
| `read_inbox_max_id`, `read_outbox_max_id`, `unread_count` | int | Your read state | SELF | Also bounds the max message id. |
| `chat_photo` | Photo | **Full-resolution** channel picture | A | Full `Photo` (sizes, video_sizes, dc_id), unlike the `ChatPhoto` on `channel`. |
| `notify_settings` | PeerNotifySettings | | SELF | |
| `exported_invite` | flags.23?ExportedChatInvite | Primary invite link | **ADM** in practice | `chatInviteExported` carries `link`, `admin_id`, `date`, `usage`, `usage_limit`, `requested`, `title`, `subscription_pricing`. |
| `bot_info` | Vector<BotInfo> | Bots in the channel | A | Reveals the automation stack. |
| `migrated_from_chat_id` / `migrated_from_max_id` | flags.4?long / int | Legacy group this supergroup came from + the migration-point message id | A | **A whole second, older archive** as a separate peer. |
| `pinned_msg_id` | flags.5?int | *Latest* pinned only | A | Full list ⇒ `messages.search` + `inputMessagesFilterPinned`. |
| `stickerset` | flags.8?StickerSet | Group sticker pack | A | Pack has an owner/short_name → another entity. |
| `available_min_id` | flags.9?int | *"Identifier of a maximum unavailable message in a channel due to hidden history"* | A | With `hidden_prehistory`, the exact floor of readable history. **Capture per-channel so a truncated archive isn't mistaken for a complete one.** |
| `folder_id` | flags.11?int | Your archive folder | SELF | |
| `linked_chat_id` | flags.14?long | **Linked discussion supergroup** (or, from a group, the associated channel) | A | The single most useful pivot — comments live there. |
| `location` | flags.15?ChannelLocation | `channelLocation#209b82db geo_point:GeoPoint address:string`; `geoPoint#b2a2f663 long:double lat:double access_hash:long accuracy_radius:flags.0?int` | A | **Geolocated supergroup: exact lat/long + street address.** |
| `slowmode_seconds` / `slowmode_next_send_date` | flags.17/18?int | Interval / when *you* may post | A / SELF | |
| `stats_dc` | flags.12?int | **DC to route stats calls to** | A | ⚠ Stats calls fail unless sent to this DC — see §8.2. |
| `pts` | int | Channel update sequence | A | Free authoritative seed for the update loop; no `getDialogs` needed. |
| `call` | flags.21?InputGroupCall | Active/scheduled voice chat | A | |
| `ttl_period` | flags.24?int | Auto-delete timer | A | **If set, messages are self-destructing — continuous capture is mandatory.** |
| `pending_suggestions` | flags.25?Vector<string> | Admin hints | ADM | |
| `groupcall_default_join_as`, `default_send_as` | flags.26/29?Peer | Default identities | SELF/ADM | `default_send_as` can leak an admin's alternate channel identity. |
| `theme_emoticon` | flags.27?string | Chat theme | A | |
| `requests_pending` | flags.28?int | Pending join requests | ADM | |
| `recent_requesters` | flags.28?Vector<long> | **User IDs of recent join requesters** | ADM | Passive leak — no importers query needed. |
| `available_reactions` | flags.30?ChatReactions | `chatReactionsNone#eafc32bc` / `chatReactionsAll#52928bca allow_custom:flags.0?true` / `chatReactionsSome#661d4037 reactions:Vector<Reaction>` | A | |
| `reactions_limit` | flags2.13?int | Unique-reaction cap | A | Default `reactions_uniq_max`=11 |
| `stories` | flags2.4?PeerStories | `peerStories#9a35e999 peer:Peer max_read_id:flags.0?int stories:Vector<StoryItem>` — **stories inlined** | A | Free story dump without any `stories.*` call. |
| `wallpaper` | flags2.7?WallPaper | Custom wallpaper | A | Boost-gated ⇒ implies level. |
| `boosts_applied` / `boosts_unrestrict` | flags2.8/9?int | **Your** boosts / boosts to bypass slowmode | SELF / A | `boosts_applied` is *yours only*, not global. |
| `emojiset` | flags2.10?StickerSet | Custom-emoji pack | A | Boost-gated. |
| `bot_verification` | flags2.17?BotVerification | | A | |
| `stargifts_count` | flags2.18?int | Gifts received | A | Monetization signal. |
| `send_paid_messages_stars` | flags2.21?long | Paid-message price | A | |
| `main_tab` | flags2.22?ProfileTab | Default profile tab | A | |
| **Flags** | | | | |
| `can_view_participants` (3) | | Participant list readable | A | |
| `participants_hidden` (2.2) | | Member list hidden | A | Only above `hidden_members_group_size_min`=100. |
| `hidden_prehistory` (10) | | Pre-join history hidden | A | Pair with `available_min_id`. |
| `can_view_stats` (20) | | **`stats.*` will work for you** | SELF | Cheapest admin-rights probe. |
| `can_view_revenue` (2.12) / `can_view_stars_revenue` (2.15) | | Revenue stats readable | SELF/ADM | |
| `antispam` (2.1), `translations_disabled` (2.3) | | | A | |
| `stories_pinned_available` (2.5) | | Has pinned stories | A | Gate for `stories.getPinnedStories`. |
| `restricted_sponsored` (2.11) | | Ads disabled | A | Requires boost level ≥ `channel_restrict_sponsored_level_min`=50 ⇒ implies level. |
| `paid_media_allowed` (2.14), `paid_reactions_available` (2.16), `stargifts_available` (2.19), `paid_messages_available` (2.20) | | Monetization surfaces | A | |
| `blocked` (22) | | An anonymous admin was blocked | A | |
| `can_set_username`, `can_set_stickers`, `can_set_location`, `can_delete_channel`, `has_scheduled`, `view_forum_as_messages` | | Capability flags | SELF | Implicit fingerprint of *your* rights. |

Source: <https://core.telegram.org/constructor/channelFull> + Layer 223 schema.

---

## 2. Messages

### 2.1 Fetch methods

| Method | Signature (L223) | Access | Notes |
|---|---|---|---|
| `messages.getHistory#4423e6c5` | `peer:InputPeer offset_id:int offset_date:int add_offset:int limit:int max_id:int min_id:int hash:long` → `messages.Messages` | **A** (public) / **M** (private); **users only** | Primary backfill, ordered by date descending. Errors: `CHANNEL_INVALID`, **`406 CHANNEL_PRIVATE`**, `CHAT_ID_INVALID`, `CHAT_NOT_MODIFIED`, `FROZEN_PARTICIPANT_MISSING`, `MSG_ID_INVALID`, `PEER_ID_INVALID`, `TAKEOUT_INVALID`. |
| `channels.getMessages#ad8c9a23` | `channel:InputChannel id:Vector<InputMessage>` | **M**; users **and** bots | **Up to 200 IDs per call.** *"This method is not limited by the channel message box size, however, very old channel/supergroup messages may still be inaccessible."* ⇒ **this, not `getHistory`, is the authoritative gap-filler.** `InputMessage` = `inputMessageID id:int`, `inputMessageReplyTo id:int`, `inputMessagePinned`, `inputMessageCallbackQuery`. |
| `messages.search#29ee847a` | `flags:# peer:InputPeer q:string from_id:flags.0?InputPeer saved_peer_id:flags.2?InputPeer saved_reaction:flags.3?Vector<Reaction> top_msg_id:flags.1?int filter:MessagesFilter min_date:int max_date:int offset_id:int add_offset:int limit:int max_id:int min_id:int hash:long` | A/M; users only | **`from_id` enumerates every message by one sender in a supergroup** — a per-user activity dump without touching the participant API. `top_msg_id` scopes to a thread/topic. |
| `messages.getSearchCounters#1bbcf300` | `flags:# peer:InputPeer saved_peer_id:flags.2?InputPeer top_msg_id:flags.0?int filters:Vector<MessagesFilter>` → `Vector<messages.SearchCounter>` (`inexact:flags.1?true filter count:int`) | A/M; users only | One call ⇒ full media-type census before downloading anything. |
| `messages.searchGlobal#4bc6589a` | `flags:# broadcasts_only:flags.1?true groups_only:flags.2?true users_only:flags.3?true folder_id:flags.0?int q filter min_date max_date offset_rate offset_peer offset_id limit` | **SELF** (your dialogs only) | ⚠ **Does NOT reach channels you haven't joined** — use `channels.searchPosts` for that. `broadcasts_only` powers the client's "Channels" tab. |
| `messages.getReplies#22ddd30c` | `peer:InputPeer msg_id:int offset_id offset_date add_offset limit max_id min_id hash` | A/M; users only | Comments / thread replies. |
| `messages.getSavedHistory` | `flags:# parent_peer:flags.0?InputPeer peer offset_id … hash` | SELF | Saved messages / monoforum topics. |

Return types — the pagination metadata matters:
```
messages.messages        messages:Vector<Message> topics:Vector<ForumTopic> chats users
messages.messagesSlice   flags:# inexact:flags.1?true count:int next_rate:flags.0?int
                         offset_id_offset:flags.2?int search_flood:flags.3?SearchPostsFlood …
messages.channelMessages#c776ba4e flags:# inexact:flags.1?true pts:int count:int
                         offset_id_offset:flags.2?int messages topics chats users
messages.messagesNotModified count:int
```
`count` = total matches, `offset_id_offset` = your position, **`pts` seeds live monitoring**.

**Pagination:** *"A limit on the number of objects to be returned, typically between 1 and 100. When 0
is provided the limit will often default to an intermediate value like ~20."* The `getHistory` page
itself states **no** maximum; **100 is Telethon's `_MAX_CHUNK_SIZE` constant, not a documented cap.**
Idiom: `offsetFromID(offset_id) + add_offset` — older-than `{offset_id:N, add_offset:0}`, newer-than
`{offset_id:N, add_offset:-limit}`, around `{offset_id:N, add_offset:-limit/2}`. `hash` gives
ETag-style caching → `*NotModified`, which **materially reduces flood pressure on re-scrapes.**
— <https://core.telegram.org/api/offsets>

### 2.2 `message` — every field

```
message#3ae56482 flags:# out:flags.1?true mentioned:flags.4?true media_unread:flags.5?true
  silent:flags.13?true post:flags.14?true from_scheduled:flags.18?true legacy:flags.19?true
  edit_hide:flags.21?true pinned:flags.24?true noforwards:flags.26?true invert_media:flags.27?true
  flags2:# offline:flags2.1?true video_processing_pending:flags2.4?true
  paid_suggested_post_stars:flags2.8?true paid_suggested_post_ton:flags2.9?true
  id:int from_id:flags.8?Peer from_boosts_applied:flags.29?int from_rank:flags2.12?string
  peer_id:Peer saved_peer_id:flags.28?Peer fwd_from:flags.2?MessageFwdHeader
  via_bot_id:flags.11?long via_business_bot_id:flags2.0?long reply_to:flags.3?MessageReplyHeader
  date:int message:string media:flags.9?MessageMedia reply_markup:flags.6?ReplyMarkup
  entities:flags.7?Vector<MessageEntity> views:flags.10?int forwards:flags.10?int
  replies:flags.23?MessageReplies edit_date:flags.15?int post_author:flags.16?string
  grouped_id:flags.17?long reactions:flags.20?MessageReactions
  restriction_reason:flags.22?Vector<RestrictionReason> ttl_period:flags.25?int
  quick_reply_shortcut_id:flags.30?int effect:flags2.2?long factcheck:flags2.3?FactCheck
  report_delivery_until_date:flags2.5?int paid_message_stars:flags2.6?long
  suggested_post:flags2.7?SuggestedPost schedule_repeat_period:flags2.10?int
  summary_from_language:flags2.11?string = Message;
```

| Field | Meaning / OSINT value | Access |
|---|---|---|
| `id` | Per-channel sequential id. **Gaps ⇒ deleted (or invisible) messages.** | A |
| `date` | Send time (unixtime UTC) | A |
| `edit_date` | **Last edit time** — proves post-publication alteration | A |
| `edit_hide` | Edited but the "edited" marker is suppressed (channels use this for silent corrections). **`edit_date` still arrives.** | A |
| `from_id:Peer` | Sender. Usually absent in broadcast channels; the real user id in supergroups. | A |
| `post_author` | **Signature of the admin who posted** — a pseudonym, often a real handle | A |
| `from_rank` (2.12) | *"In supergroups, contains sender's tag"* — the custom admin title ("Owner", "Mod"). Reveals role structure. | A |
| `from_boosts_applied` | *"Supergroups only, number of boosts user gave supergroup"* — **ties a message author to money spent.** Non-anonymous senders only. | A |
| `views` / `forwards` | Post view + **forward** counters | A |
| `replies` | see §3.2 | A |
| `reactions` | see §4.1 | A |
| `grouped_id` | Album id | A |
| `pinned`, `silent`, `post`, `noforwards`, `from_scheduled`, `invert_media`, `legacy` | Self-describing | A |
| `offline` (2.1) | *"message was sent due to scheduled action by sender"* — **automation signal** | A |
| `video_processing_pending` (2.4) | Video still transcoding — **retry later or you get nothing** | A |
| `mentioned`, `media_unread`, `out` | Relative to *your* account | SELF |
| `ttl_period` | Self-destruct seconds | A |
| `restriction_reason` | Per-message platform restriction | A |
| `via_bot_id` | **Inline bot that generated the message** — reveals tooling | A |
| `via_business_bot_id` (2.0) | Business bot acting on a user's behalf | A |
| `fwd_from` / `reply_to` / `entities` / `media` | §2.4 / §2.5 / §2.6 / §5 | A |
| `reply_markup` | Inline keyboards — **button URLs are un-previewed outbound links**, frequently the real payload of scam channels | A |
| `factcheck` — `factCheck#b89bfccf flags:# need_check:flags.0?true country:flags.1?string text:flags.1?TextWithEntities hash:long` | **Country-specific official fact-check attached to the post.** Refresh: `messages.getFactCheck peer:InputPeer msg_id:Vector<int>`. | A |
| `effect`, `quick_reply_shortcut_id`, `report_delivery_until_date`, `schedule_repeat_period`, `summary_from_language` | Minor | A/SELF |
| `paid_message_stars` (2.6) | Stars the sender paid to post | A |
| `suggested_post`, `paid_suggested_post_stars/ton` | Paid post placement inside the channel | A |

### 2.3 `messageService` and every `MessageAction`

```
messageService#7a800e0a flags:# out mentioned media_unread reactions_are_possible:flags.9?true
  silent post legacy id:int from_id:flags.8?Peer peer_id:Peer saved_peer_id:flags.28?Peer
  reply_to:flags.3?MessageReplyHeader date:int action:MessageAction
  reactions:flags.20?MessageReactions ttl_period:flags.25?int = Message;
```

**Service messages are the audit trail that survives in ordinary history — no admin rights needed.**
Highest-value actions (Layer 223 field lists verbatim):

| Action | Fields | Reveals |
|---|---|---|
| `messageActionChannelCreate` | `title:string` | **Creation event + original title** |
| `messageActionChatCreate` | `title:string users:Vector<long>` | Original title + **founding member user ids** |
| `messageActionChatAddUser` | `users:Vector<long>` | **Who was added** |
| `messageActionChatDeleteUser` | `user_id:long` | Who left / was removed |
| `messageActionChatJoinedByLink` | `inviter_id:long` | **Who invited them** — the recruitment graph |
| `messageActionChatJoinedByRequest` | — | Joined via approved request |
| `messageActionChatEditTitle` | `title:string` | **Rename history** |
| `messageActionChatEditPhoto` | `photo:Photo` | **Every historical avatar, downloadable** |
| `messageActionChatMigrateTo` | `channel_id:long` | Group → supergroup target |
| `messageActionChannelMigrateFrom` | `title:string chat_id:long` | **Predecessor group id + old title** |
| `messageActionPinMessage` / `HistoryClear` | — | Pin events (`reply_to` → the pinned msg) / history wipe |
| `messageActionSetMessagesTTL` | `flags:# period:int auto_setting_from:flags.0?long` | Auto-delete toggled, and who propagated it |
| `messageActionTopicCreate` | `flags:# title_missing:flags.1?true title:string icon_color:int icon_emoji_id:flags.0?long` | **Topic creation + creator (`from_id`)** |
| `messageActionTopicEdit` | `flags:# title:flags.0?string icon_emoji_id:flags.1?long closed:flags.2?Bool hidden:flags.3?Bool` | Topic renames/closures |
| **`messageActionBoostApply`** | `boosts:int` | **Who boosted and by how much** — `from_id` is the booster. **The non-admin path to booster identity.** Emitted to all users in **supergroups**; in **broadcast channels only to the sender**. Delayed ≤15 s for grouping. |
| `messageActionGiveawayLaunch` / `GiveawayResults` | `stars:flags.0?long` / `flags:# stars:flags.0?true winners_count:int unclaimed_count:int` | Giveaway lifecycle |
| `messageActionGiftCode` | `flags:# via_giveaway unclaimed boost_peer:flags.1?Peer days:int slug:string currency amount crypto_currency crypto_amount message:flags.4?TextWithEntities` | **Premium giftcode slug + which channel was boosted** |
| `messageActionPrizeStars` | `flags:# unclaimed stars:long transaction_id:string boost_peer:Peer giveaway_msg_id:int` | Star prize + **transaction id** |
| `messageActionGroupCall` / `GroupCallScheduled` | `flags:# call:InputGroupCall duration:flags.0?int` / `call schedule_date:int` | Call start/end + **duration**; planned livestream time |
| `messageActionInviteToGroupCall` | `call:InputGroupCall users:Vector<long>` | **User ids invited to a call** |
| `messageActionConferenceCall` | `flags:# missed active video call_id:long duration:flags.2?int other_participants:flags.3?Vector<Peer>` | **Call participants** |
| **`messageActionGeoProximityReached`** | `from_id:Peer to_id:Peer distance:int` | **Two users came within `distance` metres of each other** |
| `messageActionNewCreatorPending` / `ChangeCreator` | `new_creator_id:long` | **Ownership transfer — the new owner's user id** |
| `messageActionNoForwardsToggle` / `NoForwardsRequest` | `prev_value:Bool new_value:Bool` | Content-protection toggles |
| `messageActionStarGift` / `StarGiftUnique` | incl. `from_id:flags.11?Peer peer:flags.12?Peer to_id gift_num transferred resale_amount can_export_at` | **Gift sender/recipient + monetary value + collectible provenance** |
| `messageActionGiftPremium` / `GiftStars` / `GiftTon` | currency/amount/crypto_amount/`transaction_id` | **Payment amounts and transaction ids** |
| `messageActionPaymentSent` / `PaymentRefunded` | `currency total_amount invoice_slug charge:PaymentCharge` | Payments |
| `messageActionSuggestedPostApproval` / `Success` / `Refund` | `rejected reject_comment:flags.2?string schedule_date price:StarsAmount` | **Paid-placement negotiation, incl. the rejection comment text** |
| `messageActionCustomAction` | `message:string` | Free-text service message from unsupported layers |
| Remainder | `Empty`, `ChatDeletePhoto`, `SetChatTheme`, `SetChatWallPaper`, `ScreenshotTaken`, `ContactSignUp`, `GameScore`, `PhoneCall`, `BotAllowed`, `WebViewDataSent(Me)`, `RequestedPeer(SentMe)`, `SuggestProfilePhoto`, `TodoCompletions`, `TodoAppendTasks`, `PaidMessagesPrice`, `PaidMessagesRefunded`, `SuggestBirthday`, `StarGiftPurchaseOffer(Declined)`, `SecureValuesSent(Me)` | | 63 total in Layer 223 |

⚠ The live `/type/MessageAction` page additionally lists `messageActionPollAppendAnswer`,
`messageActionPollDeleteAnswer`, `messageActionManagedBotCreated` — **absent from the Layer 223 schema
dump**; they postdate it. — <https://core.telegram.org/type/MessageAction>

### 2.4 `messageFwdHeader` — provenance

```
messageFwdHeader#4e4df4bb flags:# imported:flags.7?true saved_out:flags.11?true
  from_id:flags.0?Peer from_name:flags.5?string date:int channel_post:flags.2?int
  post_author:flags.3?string saved_from_peer:flags.4?Peer saved_from_msg_id:flags.4?int
  saved_from_id:flags.8?Peer saved_from_name:flags.9?string saved_date:flags.10?int
  psa_type:flags.6?string = MessageFwdHeader;
```

| Field | OSINT value |
|---|---|
| `from_id:Peer` | **The original channel/user** — the primary edge for a channel-to-channel influence graph |
| `from_name:string` | Present **instead of** `from_id` when the original sender has forward-privacy on — **you still get their display name** |
| `date` | **Original publication time** — orders the true origin of a claim across channels |
| `channel_post` | **Message id in the source channel** → reconstruct `t.me/<source>/<channel_post>` and fetch the original |
| `post_author` | Signature of the source channel's admin |
| `saved_from_peer` / `saved_from_msg_id` / `saved_from_id` / `saved_from_name` / `saved_date` | Saved-messages chain — a **second hop** of provenance |
| `imported` | *"imported from a foreign chat service"* — came from a WhatsApp/other export. Signal of laundering/repackaging. |
| `psa_type` | PSA type |

In a **linked discussion group**, the auto-forwarded channel post carries `saved_from_peer` = the
channel and `saved_from_msg_id` = the channel post id — **this maps group message ↔ channel post
without a per-post `getDiscussionMessage` call.** — <https://core.telegram.org/api/discussion>

### 2.5 `messageReplyHeader` — replies & quotes

```
messageReplyHeader#6917560b flags:# reply_to_scheduled:flags.2?true forum_topic:flags.3?true
  quote:flags.9?true reply_to_msg_id:flags.4?int reply_to_peer_id:flags.0?Peer
  reply_from:flags.5?MessageFwdHeader reply_media:flags.8?MessageMedia reply_to_top_id:flags.1?int
  quote_text:flags.6?string quote_entities:flags.7?Vector<MessageEntity> quote_offset:flags.10?int
  todo_item_id:flags.11?int = MessageReplyHeader;
messageReplyStoryHeader#e5af939 peer:Peer story_id:int = MessageReplyHeader;
```
- `reply_to_top_id` = the **thread root** (for channel comments, the auto-forwarded post; for forums, the topic id).
- **`quote_text` + `quote_offset` — the quoted excerpt is embedded in the reply.** If the original is
  later deleted, the quote survives in every reply. A genuine deleted-content recovery vector.
  Cap: `quote_length_max`=1024 UTF-8.
- **`reply_media` — a copy of the replied-to message's media descriptor** likewise survives.
- `messageReplyStoryHeader` persists after the story expires.

### 2.6 `MessageEntity` — all 25 variants

`messageEntityUnknown`, `Mention`, `Hashtag`, `BotCommand`, `Url`, `Email`, `Bold`, `Italic`, `Code`,
`Pre`, `TextUrl`, `Underline`, `Strike`, `Blockquote`, `Spoiler`, `CustomEmoji`, `MentionName`,
`inputMessageEntityMentionName`, `Phone`, `Cashtag`, `BankCard`, `FormattedDate`, `DiffInsert`,
`DiffReplace`, `DiffDelete`. — <https://core.telegram.org/type/MessageEntity>

- **`messageEntityTextUrl`** carries a `url` **different from the visible text** — hyperlink masking /
  phishing detection lives here. Always extract `url`, never trust the text.
- **`messageEntityMentionName`** mentions a user **by numeric id** — deanonymizes users with no
  public username. Also works with `inputUserFromMessage`.
- **`messageEntityPhone`, `Email`, `BankCard`, `Cashtag`** — **Telegram pre-extracts your selectors.**
  No regex layer needed.
- `messageEntityCustomEmoji.document_id` → the emoji's pack → another entity.
- `Spoiler`/`Blockquote` — spoiler-hidden text is fully present in `message`.

### 2.7 `MessagesFilter` — all 18 variants

`inputMessagesFilterEmpty`, `Photos`, `Video`, `PhotoVideo`, `Document`, `Url`, `Gif`, `Voice`,
`Music`, `ChatPhotos`, `PhoneCalls`, `RoundVoice`, `RoundVideo`, `MyMentions`, `Geo`, `Contacts`,
`Pinned`, `Poll`. — <https://core.telegram.org/type/MessagesFilter>

- **`inputMessagesFilterPinned`** — the *only* way to enumerate **all** pinned messages;
  `channelFull.pinned_msg_id` gives only the latest. — <https://core.telegram.org/api/pin>
- **`inputMessagesFilterChatPhotos`** — every historical avatar change.
- **`inputMessagesFilterGeo`** / **`Contacts`** — pull only geo pins and shared contact cards
  (**phone numbers**) out of a huge channel in a handful of calls.
- Feed the whole set to `messages.getSearchCounters` first to size the job.

---

## 3. Comments, threads, forums

### 3.1 Channel comments (linked discussion group)

From <https://core.telegram.org/api/discussion>:
> *"All messages sent to the channel will also be forwarded to the linked group (with sender peer
> `from_id` equal to the peer of the linked channel); those messages will also be automatically pinned
> in the group."*

> `messages.getDiscussionMessage` — *"Get discussion message from the associated discussion group of a
> channel to show it on top of the comment section, **without actually joining the group**."*

> *"Users need not join the discussion group to read comments."* `join_to_send` restricts **writing**
> (`CHAT_GUEST_SEND_FORBIDDEN`), not reading.

**Enumerating every comment thread for every post:**
1. `channels.getFullChannel` → `linked_chat_id` (the linked `Channel` object, with access_hash,
   arrives in the same response's `chats` vector).
2. Per post `N`: `messages.getDiscussionMessage#446972fd peer:InputPeer msg_id:int`
   → `messages.discussionMessage#a6341782 flags:# messages:Vector<Message> max_id:flags.0?int
   read_inbox_max_id:flags.1?int read_outbox_max_id:flags.2?int unread_count:int chats users`.
   The returned message is the auto-forwarded copy **inside the discussion group**; take its `id`.
3. `messages.getReplies(peer=discussion_group, msg_id=<that id>)` paginated → all comments.
   (Equivalently `messages.search(peer=discussion_group, top_msg_id=<that id>)`.)
4. **Cheaper bulk alternative — use this.** `messages.getHistory` on the discussion group directly and
   bucket by `reply_to.reply_to_top_id`. Roots are identifiable by `fwd_from.saved_from_peer == channel`
   and `fwd_from.saved_from_msg_id == <channel post id>`. This is **O(total_comments/100) requests
   instead of O(posts) × O(comments/100)**.

Errors for `getDiscussionMessage`/`getReplies`: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`,
`PEER_ID_INVALID`, `TOPIC_ID_INVALID`. Users only.

### 3.2 `messageReplies`

```
messageReplies#83d60fc2 flags:# comments:flags.0?true replies:int replies_pts:int
  recent_repliers:flags.1?Vector<Peer> channel_id:flags.0?long max_id:flags.2?int
  read_max_id:flags.3?int = MessageReplies;
```
| Field | Value |
|---|---|
| `comments` | This is a channel comment section (vs a plain thread) |
| `replies` | **Total comment count** — engagement metric, zero extra calls |
| **`recent_repliers:Vector<Peer>`** | **The last few commenters' peer ids, delivered inline with the post.** Commenter identities from `messages.getHistory` alone, without ever touching the discussion group. |
| `channel_id` | Discussion supergroup id (per-post) |
| `max_id` / `replies_pts` | Latest comment id / pts of the thread root |

### 3.3 Forum topics

- `channels.getForumTopics#0de560d1 flags:# channel:InputChannel q:flags.0?string offset_date:int
  offset_id:int offset_topic:int limit:int` → `messages.forumTopics#367617d3 flags:#
  order_by_create_date:flags.0?true count:int topics:Vector<ForumTopic> messages:Vector<Message>
  chats users pts:int`. Errors: `CHANNEL_FORUM_MISSING`, `CHANNEL_INVALID`,
  `CHANNEL_MONOFORUM_UNSUPPORTED`, `CHANNEL_PRIVATE`. Users only.
- `channels.getForumTopicsByID` for specific ids.
- ```
  forumTopic#71701da9 flags:# my:flags.1?true closed:flags.2?true pinned:flags.3?true
    short:flags.5?true hidden:flags.6?true title_missing:flags.7?true id:int date:int peer:Peer
    title:string icon_color:int icon_emoji_id:flags.0?long top_message:int read_inbox_max_id:int
    read_outbox_max_id:int unread_count:int unread_mentions_count:int unread_reactions_count:int
    unread_poll_votes_count:int from_id:Peer notify_settings:PeerNotifySettings
    draft:flags.4?DraftMessage = ForumTopic;
  ```
  **`from_id` = topic creator; `date` = creation time; `title` = topic name.** A forum's topic list is
  a table of contents of the community's concerns, with authorship.
- General topic is `id=1`, non-deletable, and the only one that may be `hidden`.
- Enumerate a topic's messages via `messages.search(top_msg_id=<topic id>)` or `messages.getReplies`.
— <https://core.telegram.org/api/forum> (⚠ that page calls the methods `messages.getForumTopics*`;
the Layer 223 schema defines them under `channels.*`. The schema is authoritative.)

---

## 4. Reactions, polls, views, read receipts

### 4.1 Reactions

```
messageReactions#a339f0b flags:# min:flags.0?true can_see_list:flags.2?true
  reactions_as_tags:flags.3?true results:Vector<ReactionCount>
  recent_reactions:flags.1?Vector<MessagePeerReaction> top_reactors:flags.4?Vector<MessageReactor>
reactionCount#a3d1cb80 flags:# chosen_order:flags.0?int reaction:Reaction count:int
messagePeerReaction#8c79b63c flags:# big:flags.0?true unread:flags.1?true my:flags.2?true
  peer_id:Peer date:int reaction:Reaction
messageReactor#4ba3a95a flags:# top:flags.0?true my:flags.1?true anonymous:flags.2?true
  peer_id:flags.3?Peer count:int
```

| Item | Access | Detail |
|---|---|---|
| Aggregate counts (`results`) | **A** | Free with every message |
| **`recent_reactions`** | **A** | **Ships inline with `messages.getHistory`** — `peer_id` + `date` + emoji of recent reactors, no extra call |
| `can_see_list` | A | Tells you whether the full list is fetchable |
| **Full reactor list** — `messages.getMessageReactionsList#461b3f48 flags:# peer:InputPeer id:int reaction:flags.0?Reaction offset:flags.1?string limit:int` → `messages.messageReactionsList#31bd492d flags:# count:int reactions:Vector<MessagePeerReaction> chats users next_offset:flags.0?string` | **groups: M — broadcast channels: BLOCKED** | **`403 BROADCAST_FORBIDDEN` — *"Channel poll voters and reactions cannot be fetched to prevent deanonymization."*** Definitive: you can enumerate who reacted **in a supergroup, never in a broadcast channel.** |
| `top_reactors` | A | Paid Star-reaction leaderboard. `anonymous:flags.2?true`; **non-anonymous paid reactors are identified with `count` = stars spent** — a direct money↔identity link. |
| Refresh | `messages.getMessagesReactions peer:InputPeer id:Vector<int>` → `Updates` | M | `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID` |
| `min` on `messageReactions` | — | Your own reaction is missing; refetch with `getMessagesReactions` |
| Allowed reactions | `channelFull.available_reactions:ChatReactions` | A | |

Config: `reactions_uniq_max`=11, `reactions_user_max_default`=1 / `_premium`=3, `reactions_in_chat_max`=100.
— <https://core.telegram.org/api/reactions>, <https://core.telegram.org/method/messages.getMessageReactionsList>

### 4.2 Polls

```
poll#58747131 id:long flags:# closed:flags.0?true public_voters:flags.1?true
  multiple_choice:flags.2?true quiz:flags.3?true question:TextWithEntities
  answers:Vector<PollAnswer> close_period:flags.4?int close_date:flags.5?int
pollAnswer#ff16e2ca text:TextWithEntities option:bytes
pollAnswerVoters#3b6ddad2 flags:# chosen:flags.0?true correct:flags.1?true option:bytes voters:int
pollResults#7adf2420 flags:# min:flags.0?true results:flags.1?Vector<PollAnswerVoters>
  total_voters:flags.2?int recent_voters:flags.3?Vector<Peer> solution:flags.4?string
  solution_entities:flags.4?Vector<MessageEntity>
```
⚠ The live pages show a newer `pollResults#ba7bb15e` (with `has_unread_votes`, `can_view_stats`,
`solution_media`) and newer `poll` flags `open_answers`, `revoting_disabled`, `shuffle_answers`,
`hide_results_until_close`, `creator`, `subscribers_only`, `countries_iso2` — **post-Layer-223**.
`subscribers_only` and `countries_iso2` are notable: they restrict who may vote and **reveal the
geographic audience the author is targeting.**

**Who voted:**
`messages.getPollVotes#b86e380e flags:# peer:InputPeer id:int option:flags.0?bytes
offset:flags.1?string limit:int` → `messages.votesList#4899484e flags:# count:int
votes:Vector<MessagePeerVote> chats users next_offset:flags.0?string`
- Description: *"Get poll results for **non-anonymous polls**."*
- `403 BROADCAST_FORBIDDEN` — *"Channel poll voters and reactions cannot be fetched to prevent deanonymization."*
- `403 POLL_VOTE_REQUIRED` — *"Cast a vote in the poll before calling this method."*
  ⇒ **You must vote yourself before reading the voter list — an unavoidably intrusive act that
  changes the data you are measuring.**
- ⇒ **Net rule: voter identities only for `public_voters=true` polls in supergroups, and only after you vote.**
- **`pollResults.recent_voters:Vector<Peer>` is delivered inline for public polls without any of that.**
- Poll stats: `stats.getPollStats#c27dfa68 flags:# dark:flags.0?true peer:InputPeer msg_id:int` →
  `stats.pollStats#2999beed votes_graph:StatsGraph`, gated by `pollResults.can_view_stats`.
  **Layer 225+ — absent from Layer 223.**
— <https://core.telegram.org/method/messages.getPollVotes>, <https://core.telegram.org/api/stats>

### 4.3 Views / forwards

- `views` and `forwards` are on `message` directly (flags.10) — **A, free**.
- `messages.getMessagesViews#5784d3e1 peer:InputPeer id:Vector<int> increment:Bool` →
  `messages.messageViews` (`messageViews#455b853d flags:# views:flags.0?int forwards:flags.1?int
  replies:flags.2?MessageReplies`). ⚠ **`increment` controls whether you pollute the target's view
  counter — always pass `false` for passive collection.** Errors: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`,
  `MSG_ID_INVALID`, `PEER_ID_INVALID`. — <https://core.telegram.org/method/messages.getMessagesViews>

### 4.4 Read receipts

| Item | Detail |
|---|---|
| `messages.getMessageReadParticipants#31c1c44f peer:InputPeer msg_id:int` → `Vector<ReadParticipantDate>`; `readParticipantDate#4a4ff172 user_id:long date:int` ("When the user read the message") | **M, groups/supergroups only.** Errors: `CHAT_TOO_BIG`, `MSG_ID_INVALID`, `MSG_TOO_OLD`, `PEER_ID_INVALID`. Users only. |
| Size gate | `chat_read_mark_size_threshold` = **100**: *"Per-user read receipts … will be available in groups with an amount of participants less or equal to"* this |
| Time gate | `chat_read_mark_expire_period` = **604800** (7 d): *"read receipts for chats are only stored for … seconds after the message was sent"* |
| `messages.getOutboxReadDate#8c4bfe5d peer:InputPeer msg_id:int` → `outboxReadDate#3bb842ac date:int` | **SELF**, 1:1 private chats only. Errors: `MESSAGE_ID_INVALID`, `MESSAGE_NOT_READ_YET`, `MESSAGE_TOO_OLD`, `PEER_ID_INVALID`, `403 USER_PRIVACY_RESTRICTED`, `403 YOUR_PRIVACY_RESTRICTED`. Window `pm_read_date_expire_period` = 604800. |

### 4.5 Edit metadata

`messages.getMessageEditData#fda68d36 peer:InputPeer id:int` → `messages.messageEditData#26b5dde6
flags:# caption:flags.0?true`. Errors: `CHAT_ADMIN_REQUIRED`, `CHAT_WRITE_FORBIDDEN`,
`403 MESSAGE_AUTHOR_REQUIRED`, `MESSAGE_ID_INVALID`, `PEER_ID_INVALID`. Users only.
⚠ **This tells you whether *you* may edit — it does NOT return edit history.** The only source of
pre-edit content is `channelAdminLogEventActionEditMessage` (ADM, §8.1) or catching
`updateEditChannelMessage` live (§10).

### 4.6 Message links

- **`channels.getMessageLinks` does not exist.** Confirmed 404 on core.telegram.org and absent from
  the Layer 223 schema. The real method is
  `channels.exportMessageLink#e63fadeb flags:# grouped:flags.0?true thread:flags.1?true
  channel:InputChannel id:int` → `exportedMessageLink#5dab1af4 link:string html:string`.
  **M** (no admin error). `grouped` = "include other grouped media (for albums)"; `thread` = "also
  include a thread ID, if available" — useful for pivoting from a channel post to its comment thread.
  `html` is a ready-made embed snippet.
- **You do not need the API for this.** Public = `t.me/<username>/<id>`; private =
  `t.me/c/<channel_id>/<id>`; threads = `t.me/<username>/<thread_id>/<id>`; comments = `?comment=<id>`;
  also `?thread=`, `?single`, `?t=<media_timestamp>`, `?task=`, `?option=`. **Constructing links
  locally costs zero requests and zero flood budget — do that.** — <https://core.telegram.org/api/links>

---

## 5. Media — every `MessageMedia` kind

| Constructor | Definition (L223) | OSINT value |
|---|---|---|
| `messageMediaPhoto` | `flags:# spoiler:flags.3?true photo:flags.0?Photo ttl_seconds:flags.2?int` | Server-recompressed — see §5.2 |
| `messageMediaDocument` | `flags:# nopremium:flags.3?true spoiler:flags.4?true video:flags.6?true round:flags.7?true voice:flags.8?true document:flags.0?Document alt_documents:flags.5?Vector<Document> video_cover:flags.9?Photo video_timestamp:flags.10?int ttl_seconds:flags.2?int` | **`alt_documents`** = server-transcoded alternative video qualities (multiple distinct files per message; ignore if app-config `video_ignore_alt_documents` is true). **`video_cover`** = a cover the poster deliberately chose — an editorial signal. |
| `messageMediaGeo` | `geo:GeoPoint` | Static pin |
| `messageMediaGeoLive` | `flags:# geo:GeoPoint heading:flags.0?int period:int proximity_notification_radius:flags.1?int` | **Live location: coordinates + `heading` (direction of travel) + `period`.** Only meaningful captured live; a historical fetch gives the last known point. |
| `messageMediaVenue` | `geo:GeoPoint title:string address:string provider:string venue_id:string venue_type:string` | **Named place + street address + Foursquare/Google `venue_id`** — cross-platform pivot |
| **`messageMediaContact`** | `phone_number:string first_name:string last_name:string vcard:string user_id:long` | **A raw phone number + full name + full vCard + the Telegram user id.** The single highest-value media type. `vcard` can carry emails, orgs, addresses. |
| `messageMediaPoll` | `poll:Poll results:PollResults` | §4.2 |
| `messageMediaWebPage` | `flags:# force_large_media force_small_media manual:flags.3?true safe:flags.4?true webpage:WebPage` | §5.3 |
| `messageMediaStory` | `flags:# via_mention:flags.1?true peer:Peer id:int story:flags.0?StoryItem` | **Preserves a story reference after it expires** |
| **`messageMediaGiveaway`** | `flags:# only_new_subscribers:flags.0?true winners_are_visible:flags.2?true channels:Vector<long> countries_iso2:flags.1?Vector<string> prize_description:flags.3?string quantity:int months:flags.4?int stars:flags.5?long until_date:int` | **`channels` = every co-sponsoring channel** — a declared, machine-readable alliance graph. **`countries_iso2` = the geographic targeting.** |
| **`messageMediaGiveawayResults`** | `flags:# only_new_subscribers refunded:flags.2?true channel_id:long additional_peers_count:flags.3?int launch_msg_id:int winners_count:int unclaimed_count:int **winners:Vector<long>** months stars prize_description until_date` | **`winners` — *"Up to 100 user identifiers of the winners"*, public to anyone reading the channel.** No admin rights, no participant API. Present only when `winners_are_visible` was set. |
| `messageMediaPaidMedia` | `stars_amount:long extended_media:Vector<MessageExtendedMedia>` | Unpurchased items appear as `messageExtendedMediaPreview` (blurred thumb + dimensions only) |
| `messageMediaInvoice` | `flags:# shipping_address_requested test:flags.3?true title description photo:flags.0?WebDocument receipt_msg_id:flags.2?int currency:string total_amount:long start_param:string extended_media:flags.4?MessageExtendedMedia` | Commerce: **currency + amount** |
| `messageMediaDice` | `flags:# value:int emoticon:string game_outcome:flags.0?messages.EmojiGameOutcome` | |
| `messageMediaToDo` | `flags:# todo:TodoList completions:flags.0?Vector<TodoCompletion>` | `TodoCompletion` records **who ticked what and when** |
| `messageMediaGame`, `messageMediaVideoStream`, `messageMediaUnsupported`, `messageMediaEmpty` | | `Unsupported` ⇒ refetch at a newer layer |

### 5.1 `Document` / `Photo` and attributes

```
photo#fb197a65 flags:# has_stickers:flags.0?true id:long access_hash:long file_reference:bytes
  date:int sizes:Vector<PhotoSize> video_sizes:flags.1?Vector<VideoSize> dc_id:int = Photo;
document#8fd4c4d8 flags:# id:long access_hash:long file_reference:bytes date:int mime_type:string
  size:long thumbs:flags.0?Vector<PhotoSize> video_thumbs:flags.1?Vector<VideoSize> dc_id:int
  attributes:Vector<DocumentAttribute> = Document;
```
⚠ Note `photo` has **no `mime_type`, no `size`, no `attributes`** — structurally it cannot carry a
filename. `size` on `document` is a 64-bit `long`. `dc_id` tells you which DC to download from.

**`PhotoSize` variants and their `type` letters:**
`photoSizeEmpty#e17e23c type`, `photoSize#75c78e60 type w h size`, `photoCachedSize#21e1ad6 type w h bytes`,
`photoStrippedSize#e0b0bc2e type bytes`, `photoSizeProgressive#fa3efb95 type w h sizes:Vector<int>`,
`photoPathSize#d8214d41 type bytes`.
Letters: `s`=100×100, `m`=320×320, `x`=800×800, `y`=1280×1280, `w`=2560×2560 (all **"server-side
resized"**); `a`=160×160, `b`=320×320, `c`=640×640, `d`=1280×1280 (all **"server-side cropped"**);
`i`=stripped thumbnail; `j`=vector outline.
**`photoCachedSize` / `photoStrippedSize` / `photoPathSize` carry their bytes inline — no download
needed.** Extract them at parse time: a visual record survives even if the full file later becomes
unreachable. The `photoPathSize` SVG decoder table is published in full on /api/files.

**`VideoSize`:** `videoSize#de33b094 flags:# type w h size video_start_ts:flags.0?double`,
`videoSizeEmojiMarkup#f85c413c emoji_id background_colors`, `videoSizeStickerMarkup#da082fe`.
Types: `p`/`u` = animated profile pic (MPEG4), `v` = video preview, `f` = premium sticker effect (TGS).

**`DocumentAttribute*` — all of Layer 223:**
| Attribute | Definition | Value |
|---|---|---|
| **`documentAttributeFilename#15590068`** | `file_name:string` — "The file name" | **The original filename as uploaded.** Leaks device/camera conventions (`IMG_20240115_143022.jpg`, `WhatsApp Image …`), tool names, internal document titles, OS. **Exists only on `Document`.** |
| `documentAttributeVideo#43c57c48` | `flags:# round_message:flags.0?true supports_streaming:flags.1?true nosound:flags.3?true duration:double w:int h:int preload_prefix_size:flags.2?int video_start_ts:flags.4?double video_codec:flags.5?string` | **`video_codec`** ∈ `h264`/`h265`/`av1` — fingerprints the encoder/device. `duration` is a **double** here. |
| `documentAttributeAudio#9852f9c6` | `flags:# voice:flags.10?true duration:int title:flags.0?string performer:flags.1?string waveform:flags.2?bytes` | **`performer`/`title` = preserved ID3 metadata.** ⚠ `voice` is **flags.10**, not 0/1/2 — easy to misparse. `waveform` = bitpacked 5-bit values. |
| `documentAttributeSticker#6319d612` | `flags:# mask:flags.1?true alt:string stickerset:InputStickerSet mask_coords:flags.0?MaskCoords` | `alt` = the emoji it represents |
| `documentAttributeCustomEmoji#fd149899` | `flags:# free:flags.0?true text_color:flags.1?true alt:string stickerset:InputStickerSet` | |
| `documentAttributeImageSize#6c37c15c` | `w:int h:int` | Present when an image is sent **as a document** |
| `documentAttributeAnimated#11b58939` | — | GIF |
| `documentAttributeHasStickers#9801d2f7` | — | Often omitted from enumerations |

### 5.2 Photo vs. document — the EXIF question

**What is primary-sourced:**
- Every `PhotoSize` the server offers is documented as **"Server-side resized"** or **"Server-side
  cropped"** to a fixed bound. Server re-encoding of `messageMediaPhoto` is therefore certain.
- `photo#fb197a65` structurally has **no `mime_type`, no `size`, no `attributes`**, hence no
  `documentAttributeFilename` and nowhere for metadata to live.
- `document#8fd4c4d8` carries `mime_type`, exact `size:long`, and `documentAttributeFilename.file_name`.

**Operational rule:** record for every media item **whether it arrived as `messageMediaPhoto` or
`messageMediaDocument`** — that determines its forensic value. Collect `messageMediaDocument` payloads
byte-for-byte and run EXIF extraction on those.

⚠ **The claim "Telegram strips EXIF from photos" is NOT stated anywhere in primary documentation** —
not on /api/files, /constructor/photo, /constructor/messageMediaPhoto, in the Telethon docs, or in
TDLib's `td_api.tl` (all grepped for `exif`/`metadata`/`compress`/`re-encod`). It is a strong
inference from the two structural facts above plus universal informal reporting. **See §14.**
Note also that "documents are always byte-exact" is **false as a blanket statement** — videos sent to
large channels *are* explicitly server-transcoded into `alt_documents`.

### 5.3 Webpage previews

```
webPage#e89c45b2 flags:# has_large_media:flags.13?true video_cover_photo:flags.14?true id:long
  url:string display_url:string hash:int type:flags.0?string site_name:flags.1?string
  title:flags.2?string description:flags.3?string photo:flags.4?Photo embed_url embed_type
  embed_width embed_height duration author:flags.8?string document:flags.9?Document
  cached_page:flags.10?Page attributes:flags.12?Vector<WebPageAttribute> = WebPage;
```
- **`url` vs `display_url` mismatch = redirect/masking indicator.**
- `author` = byline of the linked article.
- **`cached_page:Page` — Telegram's Instant View stores a full server-side copy of the linked
  article.** If the origin goes offline or is edited, the IV copy persists. Fetch with
  `messages.getWebPage#8d9692a3 url:string hash:int` → `messages.WebPage` (error
  `WC_CONVERT_URL_INVALID`). **A built-in web archive.**
- `webPageAttributeStory#2e94c3e7 peer:Peer id:int story:flags.0?StoryItem` — a t.me story link
  preview embeds the story itself. Also `Theme`, `StickerSet`, `UniqueStarGift`, `StarGiftCollection`,
  `StarGiftAuction`.
- `messages.getWebPagePreview#570d6f6f flags:# message:string entities:flags.3?Vector<MessageEntity>`
  → `messages.WebPagePreview` — generate a preview for arbitrary text **without sending anything**.

---

## 6. Boosts

| Data item | Method / constructor | Access | Caveats | Source |
|---|---|---|---|---|
| Boost level, free | `channel.level:flags2.10?int` | **A** | Arrives with `contacts.resolveUsername` — **no boost call needed** | <https://core.telegram.org/constructor/channel> |
| Boost status | `premium.getBoostsStatus#042f1f61 peer:InputPeer` → `premium.boostsStatus#4959427a` | **A** | Errors: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `PEER_ID_INVALID` — **no `CHAT_ADMIN_REQUIRED`** | <https://core.telegram.org/method/premium.getBoostsStatus> |
| ↳ `level:int`, `current_level_boosts:int`, `boosts:int`, `next_level_boosts:flags.0?int`, `boost_url:string`, `my_boost:flags.2?true`, `my_boost_slots:flags.2?Vector<int>` | | **A** | `next_level_boosts` absent ⇒ max level (`boosts_channel_level_max`=100) | <https://core.telegram.org/constructor/premium.boostsStatus> |
| ↳ `gift_boosts:flags.4?int`, `premium_audience:flags.1?StatsPercentValue`, `prepaid_giveaways:flags.3?Vector<PrepaidGiveaway>` | | **ADM** | Doc: *"only returned to channel/supergroup admins."* `premium_audience` = share of subscribers on Premium. `prepaidGiveaway#b2539d54 id months quantity date`; `prepaidStarsGiveaway#9a9d77e0 id stars quantity boosts date` | same |
| **WHO boosted** | `premium.getBoostsList#60f67660 flags:# gifts:flags.0?true peer:InputPeer offset:string limit:int` → `premium.boostsList#86f8613c count:int boosts:Vector<Boost> next_offset:flags.0?string users:Vector<User>` | **ADM** | Method doc: *"(admins only)"*. Errors: **`CHAT_ADMIN_REQUIRED`**, `PEER_ID_INVALID`. Returns **full `User` objects**, not just ids. | <https://core.telegram.org/method/premium.getBoostsList> |
| ↳ per-boost | `boost#4b3e14d6 flags:# gift:flags.1?true giveaway:flags.2?true unclaimed:flags.3?true id:string user_id:flags.0?long giveaway_msg_id:flags.2?int date:int expires:int used_gift_slug:flags.4?string multiplier:flags.5?int stars:flags.6?long` | ADM | ⚠ **`user_id` is a flag** — anonymous gift/giveaway boosts arrive **without it**. Do not assume it is populated. | <https://core.telegram.org/constructor/boost> |
| Boosts by one user | `premium.getUserBoosts#39854d1f peer:InputPeer user_id:InputUser` → `premium.boostsList` | **ADM** | Doc says "(admins only)"; errors table lists only `PEER_ID_INVALID` — see §14 | <https://core.telegram.org/method/premium.getUserBoosts> |
| Your own slots | `premium.getMyBoosts` → `premium.myBoosts#9ae228e2 my_boosts:Vector<MyBoost> chats users`; `myBoost#c448415c slot:int peer:flags.0?Peer date:int expires:int cooldown_until_date:flags.1?int` | **SELF** | | <https://core.telegram.org/method/premium.getMyBoosts> |
| **Boost events in history** | `messageActionBoostApply#cc02aa6d boosts:int` (`from_id` = booster) | **M** | **The non-admin path to booster identity.** Emitted to all users in **supergroups**; in **broadcast channels only to the sender**. Delayed ≤15 s for grouping. | <https://core.telegram.org/api/boost> |
| Per-message booster count | `message.from_boosts_applied:flags.29?int` | **M** | Supergroups, non-anonymous senders only | <https://core.telegram.org/api/boost> |

---

## 7. Stories

| Data item | Method / constructor | Access | Caveats | Source |
|---|---|---|---|---|
| Active stories | `stories.getPeerStories#2c4ada50 peer:InputPeer` → `stories.peerStories#cae68768` | **A** | Errors: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`, `PEER_ID_INVALID` — no admin error | <https://core.telegram.org/method/stories.getPeerStories> |
| Profile-pinned stories | `stories.getPinnedStories#5821a5dc peer offset_id limit` → `stories.stories#63c3dd0a count stories:Vector<StoryItem> pinned_to_top:flags.0?Vector<int> chats users` | **A** | *"fetched by users who explicitly open your profile."* **Anything pinned stays harvestable indefinitely.** `stories_pinned_to_top_count_max`=3 | <https://core.telegram.org/method/stories.getPinnedStories> |
| Full story by id | `stories.getStoriesByID#5774ca74 peer id:Vector<int>` | **A** | Expands `min` / `storyItemSkipped`. Adds `STORIES_NEVER_CREATED`, `STORY_ID_EMPTY` | <https://core.telegram.org/method/stories.getStoriesByID> |
| **Expired-story archive** | `stories.getStoriesArchive#b4352016 peer offset_id limit` | **ADM/SELF** | **`CHAT_ADMIN_REQUIRED`.** *"only visible to the poster, or to channel/supergroup admins with `edit_stories` rights"* | <https://core.telegram.org/method/stories.getStoriesArchive> |
| Story albums | `stories.getAlbums#25b3eac7 peer hash` → `stories.albums#c3987a3a hash albums:Vector<StoryAlbum>`; `storyAlbum#9325705a album_id:int title:string icon_photo:flags.0?Photo icon_video:flags.1?Document` | **A** | Only `PEER_ID_INVALID`. `stories_albums_limit`=100 | <https://core.telegram.org/method/stories.getAlbums> |
| Stories in an album | `stories.getAlbumStories#ac806d61 peer album_id offset limit` | **A** | Only `PEER_ID_INVALID` | <https://core.telegram.org/method/stories.getAlbumStories> |
| Bulk liveness probe | `stories.getPeerMaxIDs#78499170 id:Vector<InputPeer>` → `Vector<RecentStory>` | **A** | Cheap across many channels at once | <https://core.telegram.org/method/stories.getPeerMaxIDs> |
| Aggregate view/reaction counts | `stories.getStoriesViews#28e16cc8 peer id:Vector<int>` → `stories.storyViews#de9eed1d views:Vector<StoryViews> users` | **A** | Errors: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `PEER_ID_INVALID`, `STORY_ID_EMPTY` — **no admin error**, unpaginated, single call | <https://core.telegram.org/method/stories.getStoriesViews> |
| ↳ `storyViews#8d595cd6` | `has_viewers:flags.1?true views_count:int forwards_count:flags.2?int reactions:flags.3?Vector<ReactionCount> reactions_count:flags.4?int **recent_viewers:flags.0?Vector<long>**` | A | `recent_viewers` = raw viewer user ids, populated only where viewers are visible to you | <https://core.telegram.org/constructor/storyViews> |
| Full viewer list | `stories.getStoryViewsList#7ed23c57 flags:# just_contacts:flags.0?true reactions_first:flags.2?true forwards_first:flags.3?true peer q:flags.1?string id offset limit` → `stories.storyViewsList#59d78fc5` | **SELF** | *"can only be used for stories posted by the current user."* Full list needs Premium; deleted after `story_viewers_expire_period`=86400 for non-Premium | <https://core.telegram.org/method/stories.getStoryViewsList> |
| ↳ `StoryView` | `storyView#b0bdeac5 blocked blocked_my_stories_from user_id:long date:int reaction:flags.2?Reaction`; `storyViewPublicForward#9083670b … message:Message`; `storyViewPublicRepost#bd74cf49 … peer_id:Peer story:StoryItem` | SELF | | <https://core.telegram.org/constructor/storyView> |
| Reaction/interaction list (channel stories) | `stories.getStoryReactionsList#b9b2881f flags:# forwards_first:flags.2?true peer id reaction:flags.0?Reaction offset:flags.1?string limit` → `stories.storyReactionsList#aa5f789c`; `storyReaction#6090d6d5 peer_id:Peer date:int reaction:Reaction` | **ADM** | *"Can only be used by channel admins."* Errors table lists only `PEER_ID_INVALID` — see §14. No *view* info, unlike the user variant | <https://core.telegram.org/method/stories.getStoryReactionsList> |
| Home feed | `stories.getAllStories#eeb0d625 next hidden state` | **SELF** | Contacts + joined channels only | <https://core.telegram.org/method/stories.getAllStories> |

**`storyItem` — every field:**
```
storyItem#edf164f1 flags:# pinned:flags.5?true public:flags.7?true close_friends:flags.8?true
  min:flags.9?true noforwards:flags.10?true edited:flags.11?true contacts:flags.12?true
  selected_contacts:flags.13?true out:flags.16?true id:int date:int from_id:flags.18?Peer
  fwd_from:flags.17?StoryFwdHeader expire_date:int caption:flags.0?string
  entities:flags.1?Vector<MessageEntity> media:MessageMedia media_areas:flags.14?Vector<MediaArea>
  privacy:flags.2?Vector<PrivacyRule> views:flags.3?StoryViews sent_reaction:flags.15?Reaction
  albums:flags.19?Vector<int> = StoryItem;
storyItemSkipped#ffadc913 flags:# close_friends:flags.8?true live:flags.9?true id date expire_date
storyItemDeleted#51e6ee4f id:int
storyFwdHeader#b826e150 flags:# modified:flags.3?true from:flags.0?Peer from_name:flags.1?string
  story_id:flags.2?int
```
⚠ The `/constructor/storyItem` parameter table and `/api/stories` show `storyItem#16a4b93c` with an
extra `music:flags.20?Document`, while `/schema` and the TL line on the same page show `#edf164f1`
without it — **all three claiming Layer 223.** Parse defensively; do not hardcode the CRC. (§14)

**`MediaArea` — the richest geo surface in the API (all A):**
| Constructor | Definition | Value |
|---|---|---|
| **`mediaAreaGeoPoint#cad5452d`** | `coordinates:MediaAreaCoordinates geo:GeoPoint address:flags.0?GeoPointAddress` | lat/long + `accuracy_radius` + **structured `geoPointAddress#de4c5d93 country_iso2:string state:flags.0?string city:flags.1?string street:flags.2?string`** |
| **`mediaAreaVenue#be82db9c`** | `coordinates geo title address provider venue_id venue_type` | `provider` currently "foursquare" — `venue_id` pivots off-platform |
| **`mediaAreaChannelPost#770416af`** | `coordinates channel_id:long msg_id:int` | **An explicit channel→channel citation edge** |
| `mediaAreaWeather#49a6549c` | `coordinates emoji:string temperature_c:double color:int` | **Leaks local temperature at post time** |
| `mediaAreaUrl#37381085` | `coordinates url:string` | `stories_area_url_max`=3 |
| `mediaAreaSuggestedReaction#14455871` | `flags:# dark flipped coordinates reaction:Reaction` | |
| `mediaAreaStarGift#5787686d` | `coordinates slug:string` | |
| `mediaAreaCoordinates#cfc9e002` | `flags:# x y w h rotation:double radius:flags.0?double` | Percentages 0–100; rotation in degrees |

---

## 8. Admin-only surfaces

### 8.1 Admin log — `channels.getAdminLog`

`channels.getAdminLog#33ddf480 flags:# channel:InputChannel q:string
events_filter:flags.0?ChannelAdminLogEventsFilter admins:flags.1?Vector<InputUser> max_id:long
min_id:long limit:int` → `channels.adminLogResults#ed8af74d events:Vector<ChannelAdminLogEvent>
chats:Vector<Chat> users:Vector<User>`

**ADM** — `403 CHAT_ADMIN_REQUIRED`. Also `CHANNEL_INVALID`, `406 CHANNEL_PRIVATE`,
`403 CHAT_WRITE_FORBIDDEN`, `MSG_ID_INVALID`. Users only.
`channelAdminLogEvent#1fad68cd id:long date:int user_id:long action:ChannelAdminLogEventAction` —
**every event carries the actor `user_id` and `date`**, and `users`/`chats` resolve them all.
If **no** `events_filter` is passed, all event types are returned.

**`channelAdminLogEventsFilter#ea107ae4` — all 20 flags:**
`join`(0) "including joins using invite links and join requests", `leave`(1), `invite`(2), `ban`(3),
`unban`(4), `kick`(5), `unkick`(6), `promote`(7), `demote`(8), `info`(9) "about, linked chat, location,
photo, stickerset, title, username, slowmode, history TTL", `settings`(10) "invites, hidden prehistory,
signatures, banned rights, forum toggle", `pinned`(11), `edit`(12), `delete`(13), `group_call`(14),
`invites`(15), `send`(16) "A message was posted in a channel", `forums`(17), `sub_extend`(18),
`edit_rank`(19).

**All 53 `ChannelAdminLogEventAction*` (Layer 223):**

| Category | Actions |
|---|---|
| **Content** | `UpdatePinned message:Message`, **`EditMessage prev_message:Message new_message:Message`** (⇒ **full pre-edit text of every edited post**), **`DeleteMessage message:Message`** (⇒ **the complete content of deleted messages**), `SendMessage message:Message`, `StopPoll message:Message` |
| **Membership** | `ParticipantJoin`, `ParticipantLeave`, `ParticipantInvite participant:ChannelParticipant`, **`ParticipantJoinByInvite flags:# via_chatlist:flags.0?true invite:ExportedChatInvite`** (**which link they used** — and the invite carries `admin_id`, who created it), **`ParticipantJoinByRequest invite:ExportedChatInvite approved_by:long`** (**who approved**), `ParticipantToggleBan prev/new:ChannelParticipant`, `ParticipantToggleAdmin prev/new`, `ParticipantEditRank user_id:long prev_rank:string new_rank:string`, `ParticipantSubExtend prev/new` |
| **Identity** | `ChangeTitle`, `ChangeAbout`, `ChangeUsername prev_value:string new_value:string`, `ChangeUsernames prev/new:Vector<string>`, `ChangePhoto prev_photo:Photo new_photo:Photo`, `ChangePeerColor`, `ChangeProfilePeerColor`, `ChangeWallpaper`, `ChangeEmojiStatus`, `ChangeEmojiStickerSet`, `ChangeStickerSet` |
| **Structure** | `ChangeLinkedChat prev_value:long new_value:long`, **`ChangeLocation prev_value:ChannelLocation new_value:ChannelLocation`** (⇒ **location history**), `ToggleForum`, `CreateTopic topic:ForumTopic`, `EditTopic prev/new`, `DeleteTopic topic:ForumTopic`, `PinTopic` |
| **Policy** | `ToggleInvites`, `ToggleSignatures`, `ToggleSignatureProfiles`, `TogglePreHistoryHidden`, `DefaultBannedRights prev/new:ChatBannedRights`, `ToggleSlowMode prev_value:int new_value:int`, `ChangeHistoryTTL`, `ToggleNoForwards`, `ToggleAntiSpam`, `ChangeAvailableReactions`, `ToggleAutotranslation` |
| **Invites** | `ExportedInviteDelete/Revoke/Edit invite:ExportedChatInvite` |
| **Calls** | `StartGroupCall`, `DiscardGroupCall`, `ParticipantMute/Unmute/Volume participant:GroupCallParticipant`, `ToggleGroupCallSetting join_muted:Bool` |

**Retention: NOT DOCUMENTED anywhere on core.telegram.org.** The widely-cited **48 hours** comes only
from the 2017 announcement blog: *"This section stores a log of all service actions taken in the group
in the last 48 hours"* and *"Recent actions in supergroups also show messages that were deleted in the
last 48 hours and the original versions of edited messages"* — <https://telegram.org/blog/admin-revolution>.
**Secondary source; treat as a lower bound and measure empirically.** (§14)

### 8.2 Statistics

⚠ **All `stats.*` calls must be sent to the DC in `channelFull.stats_dc`** — otherwise they fail.
A collection tool needs a second authorized DC session. Easy to miss.

| Method | Access | Result |
|---|---|---|
| `stats.getBroadcastStats#ab42441a flags:# dark:flags.0?true channel:InputChannel` | **ADM** — `403 CHAT_ADMIN_REQUIRED`; also `400 BROADCAST_REQUIRED` | `stats.broadcastStats#396ca5fc period:StatsDateRangeDays followers views_per_post shares_per_post reactions_per_post views_per_story shares_per_story reactions_per_story:StatsAbsValueAndPrev enabled_notifications:StatsPercentValue growth_graph followers_graph mute_graph top_hours_graph interactions_graph iv_interactions_graph **views_by_source_graph** **new_followers_by_source_graph** **languages_graph** reactions_by_emotion_graph story_interactions_graph story_reactions_by_emotion_graph recent_posts_interactions:Vector<PostInteractionCounters>` |
| `stats.getMegagroupStats#dcdf8607 flags:# dark channel` | **ADM** — `403 CHAT_ADMIN_REQUIRED`; also `400 MEGAGROUP_REQUIRED` | `stats.megagroupStats#ef7ff916 period members messages viewers posters growth_graph members_graph new_members_by_source_graph languages_graph messages_graph actions_graph top_hours_graph weekdays_graph **top_posters** **top_admins** **top_inviters** users:Vector<User>` |
| `stats.getMessageStats#b6e0a3f5 flags:# dark channel msg_id` | **ADM** | `stats.messageStats#7fe91c14 views_graph reactions_by_emotion_graph` |
| `stats.getStoryStats#374fef40 flags:# dark peer id` | ADM (inferred — §14) | `stats.storyStats#50cd067c views_graph reactions_by_emotion_graph` |
| `stats.getMessagePublicForwards` / `stats.getStoryPublicForwards` | **ADM** / ADM (inferred) | §9 |
| `stats.loadAsyncGraph#621d5fa0 flags:# token:string x:flags.0?long` | inherits | Errors `GRAPH_EXPIRED_RELOAD`, `GRAPH_INVALID_RELOAD`, `GRAPH_OUTDATED_RELOAD` |
| `payments.getStarsRevenueStats#d91ffad6 flags:# dark ton:flags.1?true peer` | ADM | Supersedes `stats.getBroadcastRevenueStats` (absent from L223). Gated by `can_view_revenue` / `can_view_stars_revenue` |

**The three OSINT-gold sub-structures:**
- **`statsGroupTopPoster#9d04af9b user_id:long messages:int avg_chars:int`** — most active members,
  message counts, **and average message length (a writing-style fingerprint)**.
- **`statsGroupTopAdmin#d7584c87 user_id:long deleted:int kicked:int banned:int`** — **per-admin
  moderation activity**: who actually enforces.
- **`statsGroupTopInviter#535f779d user_id:long invitations:int`** — **the recruiters, ranked.**

Supporting: `statsDateRangeDays#b637edaf min_date max_date`, `statsAbsValueAndPrev#cb43acde current:double
previous:double`, `statsPercentValue#cbce2fe0 part:double total:double`,
`postInteractionCountersMessage#e7058e7f msg_id views forwards reactions`,
`postInteractionCountersStory#8a480e27 story_id views forwards reactions`,
`statsGraph#8ea464b6 flags:# json:DataJSON zoom_token:flags.0?string` / `statsGraphAsync#4a27eb2d
token:string` / `statsGraphError#bedc9822 error:string`.

Gate: `channelFull.can_view_stats` (flags.20). Verbatim: *"Administrators of channels of a certain size
(the exact limit is a server-side config, returned in the `can_view_stats` flag of `channelFull`)"* —
**no numeric member threshold is published** (§14). — <https://core.telegram.org/api/stats>

### 8.3 Invite-link intelligence

| Method | Signature | Access | Reveals |
|---|---|---|---|
| `messages.getExportedChatInvites#a2b5a3f6` | `flags:# revoked:flags.3?true peer:InputPeer admin_id:InputUser offset_date:flags.2?int offset_link:flags.2?string limit:int` → `messages.exportedChatInvites#bdc62dcc count invites:Vector<ExportedChatInvite> users` | **ADM** | ⚠ **`admin_id` is required** (filters by creator). `revoked:true` recovers dead/historical links. |
| ↳ `chatInviteExported#a22cbd96` | `flags:# revoked:flags.0?true permanent:flags.5?true request_needed:flags.6?true link:string admin_id:long date:int start_date:flags.4?int expire_date:flags.1?int usage_limit:flags.2?int usage:flags.3?int requested:flags.7?int subscription_expired:flags.10?int title:flags.8?string subscription_pricing:flags.9?StarsSubscriptionPricing` | ADM | **`title` is "Custom description for the invite link, visible only to admins"** — admins routinely label links by distribution channel ("twitter", "forum X"), directly revealing recruitment vectors. `usage` = how many each link recruited. |
| `messages.getExportedChatInvite#73746f5c peer link` | → `messages.exportedChatInvite` / `messages.exportedChatInviteReplaced` | **ADM** | |
| **`messages.getChatInviteImporters#df04dd4e`** | `flags:# requested:flags.0?true subscription_expired:flags.3?true peer:InputPeer link:flags.1?string q:flags.2?string offset_date:int offset_user:InputUser limit:int` → `messages.chatInviteImporters#81b6b00a count importers:Vector<ChatInviteImporter> users` | **ADM** | **With `link` omitted this returns the full join ledger for the whole chat.** `q` requires `requested`; `SEARCH_WITH_LINK_NOT_SUPPORTED`. |
| ↳ `chatInviteImporter#8c5adfd9` | `flags:# requested:flags.0?true via_chatlist:flags.3?true user_id:long date:int about:flags.2?string approved_by:flags.1?long` | ADM | **Exactly which users joined via which link, when, the free-text bio they wrote when requesting (`about`), and which admin approved them.** |
| `messages.getAdminsWithInvites#3920e6ef peer` | → `messages.chatAdminsWithInvites#b69b72d7 admins:Vector<ChatAdminWithInvites> users`; `chatAdminWithInvites#f2ecef23 admin_id:long invites_count:int revoked_invites_count:int` | **ADM** | Per-admin link scorecard |
| `messages.hideChatJoinRequest#7fe7e815` / `hideAllChatJoinRequests#e085f4ea` | | ADM | Approval actions |
| Passive | `channelFull.requests_pending:int`, `channelFull.recent_requesters:Vector<long>` | ADM | Requester user ids without any importers query |

---

## 9. Invite links & discovery without joining

### 9.1 `messages.checkChatInvite` — what a private channel leaks

`messages.checkChatInvite#3eadb1bb hash:string = ChatInvite` — **A. No join, no membership.**
```
chatInvite#fe65389d flags:# channel:flags.0?true broadcast:flags.1?true public:flags.2?true
  megagroup:flags.3?true request_needed:flags.6?true verified:flags.7?true scam:flags.8?true
  fake:flags.9?true can_refulfill_subscription:flags.11?true title:string about:flags.5?string
  photo:Photo participants_count:int participants:flags.4?Vector<User> color:int
  subscription_pricing:flags.10?StarsSubscriptionPricing subscription_form_id:flags.12?long
  bot_verification:flags.13?BotVerification = ChatInvite;
chatInviteAlready#5a686d7c chat:Chat = ChatInvite;
chatInvitePeek#61695cb0 chat:Chat expires:int = ChatInvite;
```
Errors: `406 CHANNEL_PRIVATE`, `INVITE_HASH_EMPTY`, `406 INVITE_HASH_EXPIRED`, `INVITE_HASH_INVALID`.
**No admin or membership error.**

**Without joining, from a bare `t.me/+hash`:** `title`, `about` (full description), `photo` (full
`Photo`, downloadable), `participants_count` (exact), **`participants:Vector<User>` — real user
objects (names, usernames, photos) of members of a private channel** — plus
`verified`/`scam`/`fake`/`public`, broadcast-vs-megagroup, whether approval is needed, the `color`
palette id, and the Star subscription price.

**`chatInvitePeek#61695cb0 chat:Chat expires:int`** — `expires` is documented as *"Read-only anonymous
access to this group will be revoked at this date."* Verbatim from <https://core.telegram.org/api/invites>:
> *"messages.checkChatInvite may return chatInvitePeek only for supergroups and channels, in which case
> the user may directly fetch chat messages using **updates, `messages.getHistory` and
> `channels.getMessages`** until the time indicated by the `expires` unixtime field."*

⇒ **Temporary full read access to a PRIVATE channel's history — plus its live update stream — without
ever joining. No join event, no membership record, no service message.** Introduced in Layer 115
("Peek Channel Invite").

Invite-link regex for harvesting links from message text/entities:
`@(?:t|telegram)\.(?:me|dog)/(joinchat/|\+)?([\w-]+)@i`

`messages.importChatInvite#6c50051c hash:string` errors: `CHANNELS_TOO_MUCH`, `CHANNEL_PRIVATE`,
`INVITE_HASH_EMPTY`, `406 INVITE_HASH_EXPIRED`, `INVITE_HASH_INVALID`, `INVITE_REQUEST_SENT`,
`STARS_PAYMENT_REQUIRED`, `USERS_TOO_MUCH`, `USER_ALREADY_PARTICIPANT`, `USER_CHANNELS_TOO_MUCH`.

### 9.2 Recommendations, global search, sponsored

| Item | Method | Access | Detail | Source |
|---|---|---|---|---|
| **Similar channels** | `channels.getChannelRecommendations#25a71742 flags:# channel:flags.0?InputChannel` → `messages.chats#64ff9fd5` / `messages.chatsSlice#9cd81144 count:int chats:Vector<Chat>` | **A** | *"a list of similarly themed public channels, selected based on similarities in their **subscriber bases**."* **No `PREMIUM_ACCOUNT_REQUIRED`** — not premium-gated, only *count*-limited: `recommended_channels_limit_default`=**10**, `_premium`=**100**. Errors: `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `CHAT_NOT_MODIFIED`. Omit `channel` ⇒ recommendations from your joined set. **`messages.chatsSlice.count` = "Total number of results that were found server-side (not all are included in chats)" — a non-Premium account still learns the true degree.** | <https://core.telegram.org/method/channels.getChannelRecommendations> |
| **Global public-post search** | `channels.searchPosts#f2c4f24d flags:# hashtag:flags.0?string query:flags.1?string offset_rate:int offset_peer:InputPeer offset_id:int limit:int allow_paid_stars:flags.2?long` | **PREM** (+ Stars for `query`) | *"Globally search for posts from public channels (**including those we aren't a member of**)."* Exactly one of `hashtag`/`query`. Errors: **`403 PREMIUM_ACCOUNT_REQUIRED`**, `OFFSET_PEER_ID_INVALID`, `420 FROZEN_METHOD_INVALID`. Paginate via `next_rate` → `offset_rate`. | <https://core.telegram.org/method/channels.searchPosts> |
| ↳ paid-search quota | `channels.checkSearchPostsFlood#22567115 flags:# query:flags.0?string` → `searchPostsFlood#3e0b5b6a flags:# query_is_free:flags.0?true total_daily:int remains:int wait_till:flags.1?int stars_amount:long` | PREM | **Call before searching.** Also surfaces in `messages.messagesSlice.search_flood`. Pagination calls after the first are free. | <https://core.telegram.org/api/search> |
| **Global story search (hashtag OR GEO)** | `stories.searchPosts#d1810907 flags:# hashtag:flags.0?string area:flags.1?MediaArea peer:flags.2?InputPeer offset:string limit:int` → `stories.foundStories#e2de7737 count stories:Vector<FoundStory> next_offset chats users`; `foundStory#e87acbc0 peer:Peer story:StoryItem` | **A** | *"Globally search for stories using a hashtag or a **location media area**."* **Only documented error: `HASHTAG_INVALID`. No Premium, no payment, no membership.** `area` takes `mediaAreaGeoPoint` (must have an address) or `mediaAreaVenue`. | <https://core.telegram.org/method/stories.searchPosts> |
| **Sponsored messages** | `messages.getSponsoredMessages#3d6ce850 flags:# peer:InputPeer msg_id:flags.0?int` → `messages.sponsoredMessages#ffda656d flags:# posts_between:flags.0?int start_delay:flags.1?int between_delay:flags.2?int messages:Vector<SponsoredMessage> chats users` / `messages.sponsoredMessagesEmpty#1839490f` | **A** | ⚠ **`channels.getSponsoredMessages` is REMOVED** (schema only up to layer 192) — use `messages.getSponsoredMessages`. Cache 5 min. | <https://core.telegram.org/method/messages.getSponsoredMessages> |
| ↳ `sponsoredMessage#7dbf8673` | `flags:# recommended:flags.5?true can_report:flags.12?true random_id:bytes url:string title:string message:string entities:flags.1?Vector<MessageEntity> photo:flags.6?Photo media:flags.14?MessageMedia color:flags.13?PeerColor button_text:string **sponsor_info:flags.7?string** additional_info:flags.8?string min_display_duration:flags.15?int max_display_duration:flags.15?int` | A | **`sponsor_info` is the legally-mandated advertiser disclosure** — often a company name/registration. ⚠ The current layer **dropped `from_id`, `chat_invite`, `channel_post`, `start_param`** — advertiser attribution now survives only as `url` + free-text. | <https://core.telegram.org/constructor/sponsoredMessage> |
| Sponsored *peers* | `contacts.getSponsoredPeers#b6c8c393 q:string` → `contacts.sponsoredPeers#eb032884 peers:Vector<SponsoredPeer> chats users`; `sponsoredPeer#c69708d3 random_id:bytes peer:Peer sponsor_info:flags.0?string additional_info:flags.1?string` | **A** | Unlike `sponsoredMessage`, this **does** carry a real `peer:Peer` — the better advertiser-identification surface. | <https://core.telegram.org/api/sponsored-messages> |
| **Public forwards of a post** | `stats.getMessagePublicForwards#5f150144 channel:InputChannel msg_id:int offset:string limit:int` | **ADM** | *"Obtains a list of messages, indicating to which other public channels was a channel message forwarded."* **Confirmed admin-only — `CHAT_ADMIN_REQUIRED` is in the errors table.** → `stats.publicForwards#93037e20 flags:# count:int forwards:Vector<PublicForward> next_offset chats users`; `publicForwardMessage#1f2bf4a message:Message` / `publicForwardStory#edf3add0 peer:Peer story:StoryItem`. **Non-admin substitutes: `channels.searchPosts` (Premium) for distinctive post text, or `stories.searchPosts` (free) for stories.** | <https://core.telegram.org/method/stats.getMessagePublicForwards> |
| **Message author (monoforum)** | `channels.getMessageAuthor channel:InputChannel id:int` → `User` | **ADM** | *"obtains the original sender of a message sent by other monoforum admins to the monoforum, on behalf of the channel."* Pierces admin anonymity. | <https://core.telegram.org/method/channels.getMessageAuthor> |

### 9.3 Giveaways

| Item | Constructor | Access | Detail |
|---|---|---|---|
| Announcement | `messageMediaGiveaway` | **A** | `channels:Vector<long>` = co-sponsor graph; `countries_iso2` = targeting |
| **Winners** | `messageMediaGiveawayResults.winners:Vector<long>` | **A** | *"Up to 100 user identifiers of the winners"* — in an ordinary channel message |
| Info | `payments.getGiveawayInfo#f4239425 peer msg_id` → `payments.giveawayInfo#4367daa0 flags:# participating:flags.0?true preparing_results:flags.3?true start_date joined_too_early_date:flags.1?int admin_disallowed_chat_id:flags.2?long disallowed_country:flags.4?string` / `payments.giveawayInfoResults#e175e66f flags:# winner:flags.0?true refunded:flags.1?true start_date gift_code_slug:flags.3?string stars_prize:flags.4?long finish_date winners_count activated_count:flags.2?int` | **A** | ⚠ **Caller-relative** — reports *your own* participation; does **not** enumerate winners |
| Receipt | `messageActionGiftCode` | SELF | Delivered privately to winners |

---

## 10. Restricted channels — does the API still return content? **(VERIFIED)**

`restrictionReason#d072acb4 platform:string reason:string text:string`, present on `message`(flags.22),
`channel`(flags.9), `user`(flags.18). `platform` may be dash-concatenated (`android-ios`) or `all`;
`reason` ∈ `porno`, `terms`, `sensitive`.

**<https://core.telegram.org/api/age-verification> documents the enforcement algorithm explicitly, and
it is entirely client-side:**
- Skip a reason if it appears in app-config **`ignore_restriction_reasons`**.
- Match `platform` against your own platform string or **`restriction_add_platforms`**.
- If the only surviving reason is `sensitive`, gate on `need_age_video_verification` / `verify_age_min`
  / `verify_age_country` / `verify_age_bot_username`.
- Related: `account.setContentSettings#b574b16b flags:# sensitive_enabled:flags.0?true` and
  `account.getContentSettings#8b9b4dae` → `account.contentSettings#57e28221 flags:#
  sensitive_enabled:flags.0?true sensitive_can_change:flags.1?true` (error `403 SENSITIVE_CHANGE_FORBIDDEN`).

⇒ **The API returns the content; the client is instructed to hide it.** A read-only archiver simply
records `restriction_reason` as metadata and stores the content. There is no server-side gate. This is
why iOS builds hide `porno`-restricted channels that Android/desktop show.

---

## 11. Live-only vs. historical — and deletion detection

### 11.1 The `pts` test — the single most important distinction for an archiver

**Updates carrying `pts`/`pts_count` live in the channel message box and are replayable via
`updates.getChannelDifference` for roughly the last 100 000 pts events. Updates without `pts` are gone
the instant you miss them.**

| Update | Has `pts`? | Recoverable? |
|---|---|---|
| `updateNewChannelMessage` | ✅ | Yes, within the pts window |
| `updateEditChannelMessage#1b3f4df7 message:Message pts pts_count` | ✅ | Yes — but **only the post-edit `Message`; the pre-edit body is never retrievable** |
| `updateDeleteChannelMessages#c32d5b12 channel_id:long messages:Vector<int> pts pts_count` | ✅ | Yes — **the only unambiguous deletion evidence** |
| `updatePinnedChannelMessages#5bb98608 flags:# pinned:flags.0?true channel_id messages pts pts_count` | ✅ | Yes |
| `updateReadChannelInbox#922e6e10 … pts` | ✅ | Yes |
| **`updateChannelMessageViews#f226ac08 channel_id:long id:int views:int`** | ❌ | **Never.** Snapshot only via `message.views` / `messages.getMessagesViews` |
| **`updateChannelMessageForwards#d29a27f4 channel_id id forwards:int`** | ❌ | **Never** |
| **`updateChannelUserTyping#8c88c923 flags:# channel_id top_msg_id:flags.0?int from_id:Peer action:SendMessageAction`** | ❌ | **Never.** Pure ephemeral — and `from_id` deanonymizes discussion-group activity in real time |
| `updateReadChannelOutbox#b75f99a9 channel_id max_id` | ❌ | Never (only your own messages) |
| `updateChannelParticipant#985d3abb flags:# via_chatlist channel_id date actor_id:long user_id:long prev_participant new_participant invite qts:int` | **`qts`** | ⚠ **`qts` is the BOT sequence — a user account cannot replay it.** The historical equivalent for users is `channels.getAdminLog` (ADM). |
| `updateUserStatus` (online) | ❌ | Contacts only; `contacts.getStatuses` should be polled *"every 70000-100000 seconds"*, retry 5–10 s on failure |

Also unrecoverable historically: **`online_count`** (poll `channels.getFullChannel`), **reaction
add/remove events** (only the current aggregate persists), **stories after 24 h** unless pinned or in
the admin-only archive, and **live-location updates** (only the last point persists).

⇒ **Design implication: a channel OSINT tool must run a persistent update loop alongside its backfill,
or it structurally cannot observe deletion, edit, or retraction behaviour — exactly the behaviour that
matters most in an investigation.**

### 11.2 **Passive live updates WITHOUT joining** — the key finding

Verbatim from <https://core.telegram.org/api/updates>:
> *"This mechanism may also be used to enable passive reception of updates from channels or supergroups
> **we're not a member of**: if the specified channel or supergroup is **public**, or is private but
> temporarily available … thanks to a `chatInvitePeek`, the API will start **passively sending
> updates** … to all logged-in sessions, as long as any of the sessions continues to periodically
> invoke `updates.getChannelDifference` every `timeout` seconds."*
> *"Clients should also **limit to 10** the maximum number of channels/supergroups short-polled."*

⇒ **No join event, no membership trace, but a full live delete/edit feed.** Cap concurrency at 10
channels per session.

### 11.3 `updates.getChannelDifference`

`updates.getChannelDifference#3173d78 flags:# force:flags.0?true channel:InputChannel
filter:ChannelMessagesFilter pts:int limit:int` — users **and** bots.
- `limit`: *"How many updates to fetch, max 100000. **Ordinary (non-bot) users are supposed to pass
  10-100**."* `force` = *"skip some possibly unneeded updates and reduce server-side load."*
- `ChannelMessagesFilter` = `channelMessagesFilterEmpty#94d42ee7` or `channelMessagesFilter#cd77d957
  flags:# exclude_new_messages:flags.1?true ranges:Vector<MessageRange>`; *"This filter cannot be used
  to fetch messages older than the channel message box size."*
- Results: `updates.channelDifference#2064674e flags:# final:flags.0?true pts:int timeout:flags.1?int
  new_messages other_updates chats users`; `updates.channelDifferenceEmpty#3e11affb`;
  `updates.channelDifferenceTooLong#a4bcc6fe flags:# final timeout dialog:Dialog messages chats users`
  — *"The passed pts is too old… usually happens for updates older than **latestPts - 100000**"*, and
  its `messages` are *"the latest messages (**not starting from the passed pts**, just the latest)"*.
- Gap arithmetic: `local_pts + pts_count == pts` → apply; `>` → already applied, ignore; `<` → gap.
  *"it may be useful to wait up to 0.5 seconds"* before gap-filling, in case of reorder.
- Message box size: **~100 000 pts for channels** (~5 000 000 for the common box) — *"a server-side
  implementation detail that clients should not rely on."*
- Forced-difference triggers: `updateChannelTooLong`, `updatesTooLong`, new-session notification,
  deserialization failure, incomplete short constructor, and **"no updates for 15 minutes or longer"**.
  (`updateChannelTooLong` ≠ box overflow — it means the queue is too large to push passively.)
- Seed `pts` from `channelFull.pts` or `messages.channelMessages.pts`.

### 11.4 Detecting deletions from a backfill

- Channel message ids are a dense ascending sequence. **Gaps ⇒ deleted or invisible messages.**
  Compare `messages.channelMessages.count` against the id span.
- `channels.getMessages` returns **`messageEmpty#90a6ca84 flags:# id:int peer_id:flags.0?Peer`** —
  *"Empty constructor, non-existent message."* Verbatim from /api/updates: *"These methods will return
  placeholder `messageEmpty` constructors for **deleted or otherwise non-representable** messages, so
  that the entire fetched range is returned, in one way or another."*
- ⚠ **`messageEmpty` does NOT distinguish deleted-from-never-existed.** Gaps also arise from
  `hidden_prehistory` (`available_min_id`), from service messages you cannot see, and from
  per-user restrictions. **The only unambiguous deletion evidence is catching
  `updateDeleteChannelMessages` live or within the pts window.** Telegram's recommended structure is a
  per-channel segment tree of known-gapless message-ID ranges.
- ⚠ **Documented conflict:** /api/updates states *"`messages.getHistory` cannot be used to fill gaps in
  channels/supergroups, as it is also limited by the channel message box size"*, while the same page
  says `channels.getMessages` *"is not limited by the channel message box size."* Reported verbatim
  without resolution. **Practical rule: use `channels.getMessages` (≤200 ids/call) as the authoritative
  gap-filler and don't assume `getHistory` alone yields a provably complete range.** (§14)

---

## 12. Media download, file references, rate limits, bulk export

### 12.1 Downloading

| Item | Detail | Source |
|---|---|---|
| `upload.getFile#be5335be flags:# precise:flags.0?true cdn_supported:flags.1?true location:InputFileLocation offset:long limit:int` | Users **and** bots | <https://core.telegram.org/method/upload.getFile> |
| **offset/limit rules (no `precise`)** | `offset % 4096 == 0`; `limit % 4096 == 0`; `1048576 % limit == 0` | <https://core.telegram.org/api/files> |
| **offset/limit rules (`precise`)** | `offset % 1024 == 0`; `limit % 1024 == 0`; `limit <= 1048576` | ibid. |
| **Always** | `offset/(1024*1024) == (offset+limit-1)/(1024*1024)` — a request may **never straddle a 1 MB boundary** | ibid. |
| Locations | `inputPhotoFileLocation#40181ffe id access_hash file_reference thumb_size:string`; `inputDocumentFileLocation#bad07584 …` — **for the full document pass `thumb_size = ""`**; `inputPeerPhotoFileLocation#37257e99 flags:# big:flags.0?true peer:InputPeer photo_id:long` (**the one location a min access_hash works with**); `inputStickerSetThumb`, `inputTakeoutFileLocation`, legacy `inputFileLocation`/`inputPhotoLegacyFileLocation` (bot-API compat only) | ibid. |
| Errors | `FILE_REFERENCE_EXPIRED/INVALID/EMPTY`, `FILE_ID_INVALID`, `OFFSET_INVALID`, `LIMIT_INVALID`, `LOCATION_INVALID`, **`303 FILE_MIGRATE_X`** (*"the file … is currently stored in a different data center"*, X = DC), `420 FLOOD_WAIT_X`, `420 FLOOD_PREMIUM_WAIT_%d`, `406 FILEREF_UPGRADE_NEEDED`, `CDN_METHOD_INVALID` | ibid. |
| CDN | `upload.fileCdnRedirect#f18cda44 dc_id file_token encryption_key encryption_iv file_hashes` → `upload.getCdnFile#395f69da file_token offset limit`. `FILE_TOKEN_INVALID` ⇒ *"Continue downloading the file from the master DC using `upload.getFile`"* | <https://core.telegram.org/method/upload.getCdnFile> |
| Integrity | `upload.getFileHashes#9156982a location offset` → `fileHash#f39b035c offset limit hash:bytes` (SHA-256/part). *"clients are recommended to verify hashes for each downloaded part"* | <https://core.telegram.org/api/files> |
| **Max file size** | `upload_max_fileparts_default`=4000, `_premium`=8000; max part_size = **524288 (512 KB)**. *"the maximum file size can be extrapolated by multiplying this value by 524288"* ⇒ **~2 GB non-Premium, ~4 GB Premium** | <https://core.telegram.org/api/config> |
| Parallelism per DC | `small_queue_max_active_operations_count` / `large_queue_max_active_operations_count` — *"at most … files in parallel when downloading files smaller/bigger than 20 MB"* from the same DC | <https://core.telegram.org/api/files> |
| **Dedicated sessions** | *"large queries (`upload.getFile`, …) be handled through one or more separate sessions and separate connections."* **"Dedicated file transfer sessions on media DCs are exempt [from `AUTH_KEY_DUPLICATED`] and may always be opened in parallel."** | ibid., <https://core.telegram.org/api/errors> |

### 12.2 File references — critical for an archiver

- *"A file reference **may expire**, in which case it cannot be used in outgoing constructors: it must
  be refreshed by **refetching the message, story, etc where the media last appeared**."*
  **No TTL is documented anywhere.**
- Errors: `FILE_REFERENCE_EXPIRED`, `FILE_REFERENCE_INVALID`, plus indexed `FILE_REFERENCE_%d_*` for
  multi-media methods. Treat `_INVALID` identically to `_EXPIRED`.
- **The correct refresh for a channel archive is `channels.getMessages(channel, [msg_id])`** — exactly
  what Telegram's own published refresh action (`getMessageOp` on `fileSourceMessage#b19f4c78 flags:#
  quick_reply_shortcut_id:flags.0?int peer:long id:int`) does. **Store `(peer, msg_id)` next to every
  `file_id` at ingest — that is your entire file-source table.**
- Telegram publishes a **machine-readable map file** (JSON + TL) per layer encoding every traverser and
  refresh action, so this can be generated rather than hand-written.
- ⚠ **Telethon's auto-refresh has a narrow blast radius**: documents only, full-file only
  (`thumb_size == ''`), and only when it knows the source message. **Photo and thumbnail downloads
  re-raise `FileReferenceExpiredError`.** Build your own retry wrapper.
— <https://core.telegram.org/api/file-references>

### 12.3 Rate limits

| Item | Detail |
|---|---|
| `420 FLOOD_WAIT_%d` | *"Please wait %d seconds before repeating the action."* ⚠ In `errors.json` its method array is **empty**, which per the spec means **"errors that can be emitted by any method"**. **There is no documented per-method quota table anywhere.** |
| `420 FLOOD_PREMIUM_WAIT_%d` | Methods: `["upload.getFile"]`. *"download speed is limited because the current account does not have a Premium subscription."* *"This error can only be received when the user has uploaded tens of gigabytes or more."* Premium multiplier `upload_premium_speedup_download`=10. |
| `400 PEER_FLOOD` | *"The current account is spamreported, you cannot execute this action."* Not a timed wait — a soft account limitation. Empty method list ⇒ any method. |
| `420 SLOWMODE_WAIT_%d` | **Send-only** (`sendMessage`, `sendMedia`, `forwardMessages`, `sendMultiMedia`, `sendInlineBotResult`). Irrelevant to a read-only archiver. |
| `406 AUTH_KEY_DUPLICATED` | Session-killing — see §0.4 |
| Telethon `flood_sleep_threshold` | Default **60**. *"The threshold below which the library should automatically sleep on flood wait and slow mode wait errors (inclusive)."* Setter clamps to `min(value or 0, 24*60*60)`. Telethon also caches per-constructor flood deadlines and **pre-emptively sleeps before sending**; waits ≤3 s ignored; `FLOOD_WAIT_0` coerced to 1 s. ⚠ **A 61-second flood raises instead of sleeping — an unattended run dies.** |
| Telethon `iter_messages` | `_MAX_CHUNK_SIZE = 100`. `wait_time=None` defaults to *"1 second only if the limit is higher than 3000"*, or *"10 seconds only if the amount of IDs is higher than 300"*. ⚠ **By default Telethon sleeps 0 s between history pages — set `wait_time` explicitly.** |
| Telethon `download_media` | `MIN_CHUNK_SIZE=4096`, `MAX_CHUNK_SIZE=512*1024`; part size auto-selected: **≤100 MB → 128 KB; ≤750 MB → 256 KB; else 512 KB**. Handles `FileMigrateError` via `_borrow_exported_sender`. |
| Telethon FAQ | *"the limiting factor in the long run are `FloodWaitError`, and using **parallel download or uploads only makes them occur sooner**."* *"The best advice we can give you is to not abuse the API, like calling many requests really quickly."* Since 2023 Telegram has tightened anti-spam, *including restrictions on fetching group members*. |
| `hash` result-caching | Pass the rolling hash of seen ids ⇒ `*NotModified`, no quota paid for unchanged pages |

### 12.4 Takeout (bulk export)

`account.initTakeoutSession#8ef3eab0 flags:# contacts message_users message_chats message_megagroups
message_channels files file_max_size:flags.5?long` → `account.Takeout`. Wrap every call in
`invokeWithTakeout#aca9fd2e`. Also `messages.getSplitRanges` + `invokeWithMessagesRange`.
- *"All requests must be wrapped in an `invokeWithTakeout` constructor, **including `upload.getFile`
  calls to save files**."*
- **`420 TAKEOUT_INIT_DELAY_%d`** — *"for security reasons, you will be able to begin downloading your
  data in %d seconds. **We have notified all your devices**."*
- *"If the selected chat types are limited to public supergroups and channels … it is enough to use
  only the **last** returned message range."*
- ⚠ **Only exports *your own* data** (channels your account has joined). **Not a rate-limit bypass for
  third-party channel archiving**, and it notifies the operator's own devices — an OPSEC consideration
  for the analyst's account, not a detection risk at the target.
— <https://core.telegram.org/api/takeout>

### 12.5 Verified `help.getAppConfig` values

`help.getAppConfig#61e3f854 hash:int` → `help.appConfig#dd18782e hash:int config:JSONValue` /
`help.appConfigNotModified#7cde641d`. Pass `hash=0` first. ⚠ *"the relative value from the **default**
object must be used if `help.getAppConfig` was invoked successfully but the desired key is not
available"* — **read at runtime, never hardcode.**

| Key | Default | Meaning |
|---|---|---|
| `channels_limit_default` / `_premium` | 500 / 1000 | max channels+supergroups a user may **join** |
| `channels_public_limit_default` / `_premium` | 10 / 20 | max public channels a user may **create** |
| `recommended_channels_limit_default` / `_premium` | **10 / 100** | similar-channels list size |
| `upload_max_fileparts_default` / `_premium` | 4000 / 8000 | × 524288 ⇒ ~2 GB / ~4 GB |
| `upload_premium_speedup_download` / `_upload` | 10 / 10 | Premium speed multiplier |
| `reactions_uniq_max` | 11 | unique reactions per message |
| `reactions_user_max_default` / `_premium` | 1 / 3 | reactions one user may add |
| `reactions_in_chat_max` | 100 | `chatReactionsSome` cap |
| `caption_length_limit_default` / `_premium` | 1024 / 4096 | media caption UTF-8 length |
| `quote_length_max` | 1024 | reply-quote length |
| `message_animated_emoji_max` | 100 | custom emoji per message |
| `chat_read_mark_size_threshold` | **100** | group size for per-user read receipts |
| `chat_read_mark_expire_period` / `pm_read_date_expire_period` | 604800 / 604800 | 7-day receipt retention |
| `hidden_members_group_size_min` | 100 | threshold to hide the member list |
| `ignore_restriction_reasons` | `[]` | reasons the client **must ignore** |
| `restriction_add_platforms` | array | extra platform ids for parsing `restrictionReason` |
| `video_ignore_alt_documents` | false | if true, ignore `alt_documents` |
| `boosts_channel_level_max` | 100 | max boost level |
| `boosts_per_sent_gift` / `giveaway_boosts_per_premium` | 3 / 4 | |
| `channel_restrict_sponsored_level_min` | 50 | boost level to disable ads |
| `channel_autotranslation_level_min` | 3 | |
| `story_caption_length_limit_default` / `_premium` | 200 / 2048 | |
| `story_expiring_limit_default` / `_premium` | 1 / 100 | max active stories |
| `stories_sent_weekly_limit_default` / `_premium` | 3 / 700 | |
| `stories_sent_monthly_limit_default` / `_premium` | 10 / 3000 | |
| `story_viewers_expire_period` | 86400 | when the exact viewer list is hidden from a non-Premium poster |
| `stories_pinned_to_top_count_max` | 3 | |
| `stories_albums_limit` / `stories_album_stories_limit` | 100 / 1000 | |
| `stories_area_url_max` | 3 | URL media areas per story |
| `stories_stealth_past_period` / `_future_period` / `_cooldown_period` | 300 / 1500 / 10800 | stealth-mode windows |
| `stories_changelog_user_id` | 777000 | official feature-update story poster |
| `stories_venue_search_username` / `weather_search_username` | foursquare / StoryWeatherBot | |
| `small_queue_max_active_operations_count` / `large_queue_…` | — | parallel downloads per DC (<20 MB / >20 MB) |
| `need_age_video_verification`, `verify_age_min`, `verify_age_country`, `verify_age_bot_username` | — | age-gate for `reason == "sensitive"` |
| `freeze_since_date`, `freeze_until_date`, `freeze_appeal_url` | — | account-freeze state |

MTProto-level config: `help.getConfig#c4f9186b` → `config#cc1a241e … chat_size_max megagroup_size_max
forwarded_count_max caption_length_max message_length_max webfile_dc_id tmp_sessions:flags.0?int
channels_read_media_period edit_time_limit revoke_time_limit …`, *"should be manually refreshed
immediately upon receival of an `updateConfig` update."*

---

## 13. Recommended collection order

1. `contacts.resolveUsername` → `Channel`. **Capture `date` (= creation date) and `level` BEFORE joining.**
2. `channels.getFullChannel` → `ChannelFull` + linked discussion group + bots + inlined `stories`
   (one call, huge payload). Record `pts`, `stats_dc`, `available_min_id`, `hidden_prehistory`,
   `ttl_period`. Remember the 60 s cache TTL.
3. `channels.getChannelRecommendations` → the subscriber-overlap graph (cheap, membership-independent;
   keep `messages.chatsSlice.count` even if truncated).
4. `messages.getSearchCounters` with all 18 filters → census before downloading anything.
5. **Start `updates.getChannelDifference` from `channelFull.pts` immediately** (limit 10–100, ≤10
   channels short-polled per session) — this works without joining for public channels, and is the
   only way to observe deletions/edits.
6. `messages.getHistory` paginated at limit≈100, newest→oldest, with `hash` caching on re-runs, and an
   explicit inter-page delay. Everything in §2.2/§2.4/§2.5/§3.2/§4.1 arrives **inline** — `views`,
   `forwards`, `recent_reactions`, `recent_repliers`, full `fwd_from` provenance. **Do not make
   per-message calls for these.**
7. `messages.search` + `inputMessagesFilterPinned` (all pinned), `ChatPhotos` (avatar history), `Geo`,
   `Contacts`, `Url`.
8. If `linked_chat_id`: `messages.getHistory` on the discussion group, bucket by
   `reply_to.reply_to_top_id` — **not** per-post `getDiscussionMessage`.
9. If `forum`: `channels.getForumTopics`, then per-topic `messages.search(top_msg_id=…)`.
10. Stories: `stories.getPinnedStories` + `stories.getStoriesViews` (both non-admin); harvest
    `media_areas` for geo.
11. Media: store `(peer, msg_id)` with every `file_id`; download **serially** on a dedicated media-DC
    session; verify with `upload.getFileHashes`; extract inline `photoStrippedSize` bytes at parse time.
12. Gap-fill with `channels.getMessages` (≤200 ids/call), distinguishing `messageEmpty`.

---

## 14. Surprising / high-value OSINT findings

1. **`chatInvitePeek` — read a PRIVATE channel without joining.** `messages.checkChatInvite` on a
   `t.me/+hash` may return `chatInvitePeek chat:Chat expires:int`, after which *"the user may directly
   fetch chat messages using **updates, `messages.getHistory` and `channels.getMessages`**"* until
   `expires`. No join event, no membership record, no service message. **Nothing else in the API comes
   close for low-visibility collection.**
2. **Passive live updates on a PUBLIC channel without joining.** *"if the specified channel or
   supergroup is public … the API will start passively sending updates … as long as any of the sessions
   continues to periodically invoke `updates.getChannelDifference` every `timeout` seconds."* A full
   live delete/edit feed with **no membership trace**. Cap: 10 channels per session.
3. **`messages.checkChatInvite` leaks a member preview.** Even without a peek, a bare invite hash yields
   `title`, `about`, downloadable `photo`, exact `participants_count`, and **`participants:Vector<User>`
   — real user objects** — for a private channel you have never joined.
4. **`channels.getChannelRecommendations` is a subscriber-overlap graph, and it is NOT Premium-gated.**
   *"selected based on similarities in their **subscriber bases**."* Telegram computes and hands over
   the audience-overlap network you would otherwise need the participant API to build. Premium raises
   the yield 10 → 100 channels, but **`messages.chatsSlice.count` gives a non-Premium account the true
   total anyway** — the truncated list is the edges, `count` is the degree.
5. **`stories.searchPosts` searches all public stories by GEOGRAPHIC AREA, free.** *"Globally search
   for stories using a hashtag or a **location media area**."* Its only documented error is
   `HASHTAG_INVALID` — no Premium, no Stars, no membership. Pass a `mediaAreaGeoPoint` or
   `mediaAreaVenue` and get every public story tagged at that place. **Area-to-people querying.**
   Its sibling `channels.searchPosts` is `403 PREMIUM_ACCOUNT_REQUIRED` and metered in Stars.
6. **`channels.searchPosts` reaches channels you are not in** — *"including those we aren't a member
   of"* — and is the **non-admin workaround for the admin-only `stats.getMessagePublicForwards`**:
   search distinctive text from a post to find who reposted it. Premium-gated; hashtag mode may be
   free (§15). Check quota first with `channels.checkSearchPostsFlood`.
7. **`messageMediaGiveawayResults.winners` is a public list of up to 100 user IDs.** Anyone reading the
   channel gets them — no admin rights, no participant API. And `messageMediaGiveaway.channels` is an
   explicit, machine-readable **co-sponsorship graph**. Scraping history for these two media types
   yields a member-adjacent identity set for free.
8. **`messageActionBoostApply` is the non-admin path to booster identity.** `premium.getBoostsList` is
   *"(admins only)"* with `CHAT_ADMIN_REQUIRED`, but the service message appears in ordinary history
   with `from_id` = the booster. ⚠ Emitted to **all users in supergroups**, but in **broadcast channels
   only to the sender**. And even for admins, `boost.user_id` is a *flag* — anonymous gift/giveaway
   boosts arrive without it.
9. **Reaction and poll deanonymization is asymmetric by channel type.** Both
   `messages.getMessageReactionsList` and `messages.getPollVotes` return
   **`403 BROADCAST_FORBIDDEN` — *"Channel poll voters and reactions cannot be fetched to prevent
   deanonymization"*** in broadcast channels, but work in supergroups. `getPollVotes` additionally
   requires **`POLL_VOTE_REQUIRED` — you must vote yourself first**, an intrusive act that changes the
   data you are measuring.
10. **`recent_reactions` and `recent_repliers` arrive free inside `messages.getHistory`.** Even where
    the full lists are blocked, every post carries `MessagePeerReaction` (peer + emoji + **timestamp**)
    and `messageReplies.recent_repliers`. Across thousands of posts this accumulates into a substantial
    participant map — sampled, but obtained without the participant API and without tripping its
    2023-era restrictions.
11. **Quotes preserve deleted content.** `messageReplyHeader.quote_text` (+ entities, offset) and
    `reply_media` embed a copy of the replied-to message *inside the reply*. When the original is
    deleted, the excerpt survives in every reply that quoted it.
12. **Instant View is a built-in web archive.** `webPage.cached_page:Page` is Telegram's server-side
    copy of a linked article, retrievable via `messages.getWebPage`. It outlives edits and takedowns at
    the origin.
13. **`fwd_from.from_name` partially defeats forward privacy.** When the original sender has forward
    privacy on, `from_id` is withheld but **`from_name` (their display name) is still delivered** — and
    `fwd_from.channel_post` gives the exact message id in the source channel.
14. **Telegram pre-extracts your selectors.** `messageEntityPhone`, `Email`, `BankCard`, `Cashtag`,
    `MentionName` (a user **by numeric id**, even with no username) and `TextUrl` (the **real** URL
    behind masked link text) mean no regex layer is needed — and `mentionName` deanonymizes usernameless
    users.
15. **`channel.date` is the creation date only until you join.** *"Date user joined or channel creation
    date."* Capture it during resolution; joining destroys it. Likewise `channel.level` gives boost
    level free — no `premium.getBoostsStatus` call needed.
16. **`migrated_from_chat_id` opens a second, older archive.** Supergroups migrated from basic groups
    retain a pointer to the predecessor peer — a whole separate history most collectors never fetch.
17. **`messages.getMessagesViews(increment=false)`.** Passing `true` (or letting a library default)
    actively contaminates the target's metrics and marks your presence.
18. **Restriction is enforced entirely client-side — now VERIFIED.**
    <https://core.telegram.org/api/age-verification> documents the filter algorithm as a client-side
    procedure driven by `ignore_restriction_reasons` and `restriction_add_platforms`. **The API returns
    the content.** An archiver records `restriction_reason` as metadata and stores the payload.
19. **One non-admin `channels.getFullChannel` yields** `location` (lat/long + street address),
    `online_count` (real-time), `admins_count`, `about`, `linked_chat_id`, inlined `stories`, `pts`,
    `available_min_id`, and `ttl_period`.
20. **`messageActionGeoProximityReached from_id to_id distance`** records that two specific users came
    within a measured distance of each other — a physical-world colocation event, in plain history.
21. **Story media areas are the richest geo surface in the API.** `mediaAreaGeoPoint` gives lat/long +
    `accuracy_radius` + structured `geoPointAddress{country_iso2, state, city, street}`;
    `mediaAreaVenue` gives a Foursquare `venue_id` that pivots off-platform;
    **`mediaAreaChannelPost{channel_id, msg_id}` is an explicit channel→channel citation edge**;
    `mediaAreaWeather.temperature_c` leaks local conditions at post time.
22. **`stories.getPinnedStories` and `stories.getStoriesViews` carry no admin error** — anything a
    channel pinned stays harvestable indefinitely, complete with `recent_viewers` user ids. Only the
    *expired* archive (`stories.getStoriesArchive`) is `CHAT_ADMIN_REQUIRED`.
23. **Admin log = total recall.** `channelAdminLogEventActionDeleteMessage message:Message` and
    `...EditMessage prev_message new_message` return the **entire deleted/pre-edit message object**.
    `ParticipantJoinByInvite` chains *user → link → link-creating admin*; `ParticipantJoinByRequest`
    adds `approved_by`.
24. **`stats.getMegagroupStats` is a de-anonymized social graph.** `statsGroupTopPoster{user_id,
    messages, avg_chars}` (message volume **plus a writing-style fingerprint**),
    `statsGroupTopAdmin{user_id, deleted, kicked, banned}` (**who actually enforces**), and
    `statsGroupTopInviter{user_id, invitations}` (**the recruiters, ranked**) — with
    `stats.megagroupStats.users` resolving every id to a full `User` in the same response.
25. **`messages.getChatInviteImporters` with `link` omitted returns the whole chat's join ledger** —
    per user: which link, when, the **free-text bio they wrote when requesting** (`about`), and
    **which admin approved them**. And `chatInviteExported.title` is *"visible only to admins"* — admins
    label links by distribution channel ("twitter", "forum X"), directly exposing recruitment vectors.
26. **`sponsoredMessage.sponsor_info`** carries the legally-mandated advertiser disclosure. ⚠ But the
    current layer **dropped `from_id`/`chat_invite`/`channel_post`** — `contacts.getSponsoredPeers`
    (which still returns a real `peer:Peer`) is now the better advertiser-identification surface.
27. **`channels.getMessageLinks` does not exist** (404, and absent from the schema). The real method is
    `channels.exportMessageLink` — and even that is unnecessary: t.me links are constructible offline
    from `(username | c/<id>, msg_id)` at zero flood cost.
28. **`documentAttributeFilename` is the only filename source in the entire API**, and it exists only on
    `Document`. `photo` structurally cannot carry one. This single flag decides the forensic value of
    every media item.
29. **`photoStrippedSize` / `photoCachedSize` / `photoPathSize` carry their bytes inline** — a visual
    record of every media item at parse time, with zero downloads and zero flood cost, surviving even
    if the full file later becomes unreachable.
30. **`AUTH_KEY_DUPLICATED` can permanently kill a session** if you exceed `tmp_sessions` on the main
    DC — but **media-DC file-transfer sessions are explicitly exempt**. Parallelise downloads there,
    never on the main connection.

---

## 15. Unverified / uncertain

**Resolved since first draft:** restriction enforcement (now VERIFIED via /api/age-verification, §10);
`stats.getMessagePublicForwards` access (VERIFIED admin-only); `premium.getBoostsList` access
(VERIFIED admin-only); `channels.getMessageLinks` non-existence (VERIFIED 404 + schema).

1. **EXIF stripping on `messageMediaPhoto`.** No statement exists on core.telegram.org (/api/files,
   /constructor/photo, /constructor/messageMediaPhoto), in the Telethon docs, or in TDLib's `td_api.tl`
   (all grepped for `exif`/`metadata`/`compress`/`re-encod`). What **is** primary-sourced: every
   `PhotoSize` is "server-side resized/cropped", and `photo` structurally has no filename/mime/size
   field. The nearest official-Telegram-but-not-API material is bugs.telegram.org threads
   (c/24370, c/5887, c/62277) — **secondary**. Verify empirically before relying on it.
2. **"Documents preserve original bytes exactly."** Strongly implied but never guaranteed — and
   **false as a blanket statement**, since videos in large channels *are* transcoded into
   `alt_documents`. Verify per media type with SHA-256 round-trips.
3. **Reading a PUBLIC channel's history without joining.** No single primary sentence states it. The
   evidence is indirect but consistent: `CHANNEL_PRIVATE` is *"You haven't joined this
   channel/supergroup"*; `chatInvitePeek` is described as granting `getHistory` *because* a private
   channel is otherwise inaccessible; `getDiscussionMessage` explicitly works *"without actually
   joining"*; `channels.getFullChannel` has no member/admin error; and /api/updates explicitly
   distinguishes **public** channels (passive updates, no membership) from private ones (need a peek).
   High confidence, formally unverified.
4. **Admin log retention.** Not documented on core.telegram.org at all. The 48-hour figure comes only
   from <https://telegram.org/blog/admin-revolution> (2017): *"a log of all service actions taken in the
   group in the last 48 hours"* / *"messages that were deleted in the last 48 hours and the original
   versions of edited messages."* **Secondary, and nine years old.** Measure empirically.
5. **Which specific admin right unlocks the admin log.** Neither the method page nor /api/rights names a
   `chatAdminRights` flag; only `403 CHAT_ADMIN_REQUIRED` and Telethon's "you must be an administrator".
   Whether a limited admin qualifies is untested.
6. **Minimum member count for statistics.** Only *"channels of a certain size (the exact limit is a
   server-side config, returned in the `can_view_stats` flag of `channelFull`)"* — **no number, no
   appConfig key.** Telethon's docstring claims "for megagroups, this requires at least 500 members" —
   secondary. Also unverified: whether `can_view_stats` is the *precise* predicate gating `stats.*`
   (the method pages say `CHAT_ADMIN_REQUIRED`, not `can_view_stats`).
7. **`stats.getStoryStats` and `stats.getStoryPublicForwards` access level.** Neither errors table
   contains `CHAT_ADMIN_REQUIRED`, unlike every sibling. Admin-gating is inferred from the namespace.
   ⚠ **If `getStoryPublicForwards` is genuinely open, it is the highest-value surface in this report —
   test it first.**
8. **`premium.getUserBoosts` and `stories.getStoryReactionsList` enforcement.** Both say "admins only"
   in prose; both list only `PEER_ID_INVALID` in errors. Whether a non-admin gets an error or an empty
   list is untested.
9. **Whether `channels.searchPosts` in `hashtag` mode requires Premium.** The 403
   `PREMIUM_ACCOUNT_REQUIRED` is listed for the method as a whole, but /api/search describes the
   Premium-plus-Stars economics only for the `query` path and treats hashtag mode separately.
10. **`messages.getHistory` limit ceiling.** The method page states no maximum; /api/offsets says only
    *"typically between 1 and 100"*. **100 is Telethon's constant, not a documented cap.** Values >100
    may be silently clamped — untested.
11. **No documented daily cap or per-method quota exists.** `FLOOD_WAIT`'s method array in `errors.json`
    is empty = "any method". `FLOOD_PREMIUM_WAIT` is the only volume-linked signal and is described
    qualitatively ("tens of gigabytes or more"). **Any specific rate number you encounter online is
    unsourced.** All pacing must be adaptive.
12. **`file_reference` lifetime.** No TTL documented. Community figures (hours to ~a day) are unsourced.
13. **Documented conflict on gap-filling.** /api/updates says *"`messages.getHistory` cannot be used to
    fill gaps in channels/supergroups, as it is also limited by the channel message box size"*, while
    the same page says `channels.getMessages` *"is not limited by the channel message box size."*
    Reported verbatim, unresolved.
14. **`storyItem` CRC/`music` field conflict.** `/schema` and the TL line on `/constructor/storyItem`
    give `storyItem#edf164f1` **without** `music`; the parameter table on that same page **and**
    `/api/stories` give `storyItem#16a4b93c … music:flags.20?Document` — **all claiming Layer 223.**
    Parse defensively; do not hardcode the CRC.
15. **Layer skew across doc pages.** `messageActionPollAppendAnswer`, `PollDeleteAnswer`,
    `ManagedBotCreated`; the newer `poll` flags (`open_answers`, `revoting_disabled`,
    `shuffle_answers`, `hide_results_until_close`, `creator`, `subscribers_only`, `countries_iso2`);
    `pollResults#ba7bb15e`; and `stats.getPollStats` (documented "as of layer 225") are all **absent
    from the Layer 223 schema dump**. Confirm against whatever layer your client library implements.
16. **`kicked_count` / `banned_count` visibility to non-admins.** Both sit behind `flags.2` with no
    documented restriction, and `getFullChannel` has no admin error — so they *should* be public.
    Whether the server populates them for non-admins is untested.
17. **`channelFull.exported_invite` for non-admins.** Marked "ADM in practice" by inference from the
    invite-management methods all being admin-gated; **not documented** on the `channelFull` page.
18. **`chatInvitePeek` window length and trigger conditions.** Only the absolute `expires` unixtime is
    documented — not the duration, not whether re-calling `checkChatInvite` renews it, and not the
    server-side condition that decides `chatInvitePeek` vs plain `chatInvite`.
19. **`chatInviteExported.requested` semantics.** Documented verbatim as *"Number of users that have
    already used this link to join"* — which duplicates `usage` and contradicts both its name and the
    `request_needed` flow (where it should count *pending* requests). Likely a docs bug.
20. **`stats.getBroadcastRevenueStats` field-level detail.** Absent from the Layer 223 schema; its
    constructor page renders no fields. Only its errors table is documented. Live equivalent:
    `payments.getStarsRevenueStats`.
21. **`/api/forum` naming.** That page says `messages.getForumTopics` / `messages.getForumTopicsByID`;
    the schema defines them under `channels.*`. The schema is authoritative; the prose page is stale.
22. **appConfig values in §12.5 are the published defaults**, not necessarily what your account
    receives. They are server-configurable — read at runtime.
