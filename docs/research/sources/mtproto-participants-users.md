# Telegram MTProto — Participants & Per-User Profile Data (OSINT capability survey)

**Research vector:** participants / members / per-user profile data, via the official MTProto **user-account** API (as used by Telethon / TDLib).
Messages and channel metadata are covered by a separate agent and are deliberately out of scope here.

**Schema baseline: Layer 223** (core.telegram.org, fetched 2026-08-20). Telethon's `v1` branch is pinned at **layer 222**.

**Method.** Every claim below is sourced. Where core.telegram.org is silent or ambiguous, I went to **TDLib source** (`github.com/tdlib/td`) — Telegram's own official client library, which encodes the real access rules — and to Telethon's issue tracker for observed behaviour over time. Claims I could not establish from a primary or authoritative source are marked **UNVERIFIED** and collected in a section at the end. **Nothing here was executed against a live account**, so anything described as "the server returns X" is inference from documentation plus client source unless a cited issue reports it empirically.

---

## The three facts that should shape the design

1. **For a broadcast channel, a non-admin subscriber gets nothing** — not the subscriber list, not even the admin list. TDLib refuses the admin query client-side with *"Administrator list is inaccessible"*, and *"Group members are hidden by default in channels."* Only `participants_count` is public. All real person-data on a channel must come from the **linked discussion group** and from message-adjacent vectors.
2. **The classic "10,000 members" figure is obsolete.** In 2024 a 78,000-member megagroup returned **12 users**. When "Hide members" is on, non-admins receive **only administrators and bots** — not an empty list, which is what most third-party write-ups incorrectly claim. Read `channelFull.participants_hidden` / `can_view_participants` **first** and branch, rather than burning flood budget on a sweep that cannot succeed.
3. **Privacy is enforced by omitting fields, not by returning errors.** `users.getFullUser` has no privacy-denial error. Absence *is* the signal — and several fields let you distinguish "not set" from "hidden from you" (`fallback_photo`, `by_me`, `private_forward_name`). A tool that models fields as present/absent instead of present / absent-not-set / absent-privacy will systematically misreport targets.

---

## Master table — data items, access, caveats

Access legend: **anyone** = any account that can resolve the peer · **member** = must have joined · **admin** = channel/group admin rights · **privacy-gated** = depends on the target's `account.setPrivacy` rules · **self** = only for the logged-in account · **bot** = bot accounts only.

### Participant enumeration

| Data item | Method / constructor | Access level | Caveats | Source URL |
|---|---|---|---|---|
| Subscriber/member count | `channelFull.participants_count` | **anyone** | Public even when the list is hidden | https://core.telegram.org/constructor/channelFull |
| Online-user count | `messages.getOnlines` → `chatOnlines{onlines}` | **member** | No `CHAT_ADMIN_REQUIRED`; works with hidden members. Aggregate only | https://core.telegram.org/method/messages.getOnlines |
| Is the list fetchable at all | `channelFull.can_view_participants`, `.participants_hidden` | member | **Branch on these before enumerating** | https://core.telegram.org/constructor/channelFull |
| Member list — supergroup | `channels.getParticipants` + `channelParticipantsRecent` | **member** | 200/request; hard server cap far below the true total; only admins+bots if hidden | https://core.telegram.org/method/channels.getParticipants |
| Member list — broadcast | `channels.getParticipants` (any filter) | **admin only** | 403 `CHAT_ADMIN_REQUIRED` for subscribers | https://core.telegram.org/method/channels.getParticipants |
| Admin list — supergroup | `+ channelParticipantsAdmins` | **member** | Returned even when members are hidden | https://core.telegram.org/type/ChannelParticipantsFilter |
| Admin list — broadcast | `+ channelParticipantsAdmins` | **admin only** | TDLib: *"Administrator list is inaccessible"* | tdlib/td `DialogParticipantManager.cpp` |
| Bots in the chat | `+ channelParticipantsBots` | member | Returned even when members are hidden | https://core.telegram.org/type/ChannelParticipantsFilter |
| Members who are your contacts | `+ channelParticipantsContacts(q)` | member | Relative to *your* contact list | same |
| Name/prefix search | `+ channelParticipantsSearch(q)` | member | The basis of the alphabet sweep | same |
| Restricted users | `+ channelParticipantsBanned(q)` | **admin** | Naming inverted — this is *restricted*, not banned | same |
| Kicked/banned users | `+ channelParticipantsKicked(q)` | **admin** | Gives `kicked_by` + ban date | same |
| Mentionable members **+ non-participant commenters** | `+ channelParticipantsMentions(q, top_msg_id)` | member | *"return even non-participant users that replied to a specific thread through the comment section of a channel"* | https://core.telegram.org/constructor/channelParticipantsMentions |
| Single-user membership + join date | `channels.getParticipant` | member (not gated client-side by TDLib) | **Membership oracle** — works per-user where bulk enumeration doesn't | https://core.telegram.org/method/channels.getParticipant |
| Basic-group full roster w/ inviter **and** join date | `messages.getFullChat` → `chatParticipant{user_id, inviter_id, date, rank}` | member | ≤200 members; strictly more data than supergroups give | https://core.telegram.org/api/channel |

### Per-participant fields

| Data item | Constructor / field | Access level | Caveats | Source URL |
|---|---|---|---|---|
| **Join date** — ordinary member | `channelParticipant.date` | member | "Date joined" — present for **every** enumerable member | https://core.telegram.org/constructor/channelParticipant |
| Join date — admin | `channelParticipantAdmin.date` | member | Doc: *"When did the user join"* (join, not promotion) | https://core.telegram.org/constructor/channelParticipantAdmin |
| Join date — creator | — | — | **`channelParticipantCreator` has no `date` field at all** | https://core.telegram.org/schema |
| Ban date + who banned | `channelParticipantBanned.date`, `.kicked_by` | **admin** | `date` is the ban date, not a join date. `peer` may be a channel | https://core.telegram.org/schema |
| Who promoted an admin | `channelParticipantAdmin.promoted_by` | member | Always present on admins | same |
| Who invited a member | `.inviter_id` | **self only** for channels | On `channelParticipantAdmin`, `inviter_id` shares flags.1 with `self` ⇒ **only ever populated for you**. `channelParticipant` has no `inviter_id`. Basic groups *do* expose it for everyone | https://core.telegram.org/schema |
| Custom admin title / tag | `.rank` | member | Present on ordinary participants too; often leaks org role or real name | https://core.telegram.org/api/rank |
| Paid-subscription expiry | `channelParticipant.subscription_until_date` | member | Identifies **paying** subscribers and when they lapse | https://core.telegram.org/constructor/channelParticipant |
| Joined via approved request | `channelParticipantSelf.via_request` | **self only** | | https://core.telegram.org/schema |

### Member discovery (detail and ranking in the dedicated section below)

| Data item | Method / constructor | Access level | Caveats | Source URL |
|---|---|---|---|---|
| Message authors | `messages.getHistory` / `messages.search`, `message.from_id` | anyone (public) / member | Arrive as `min` users — must store `(chat, msg_id)` provenance | https://core.telegram.org/api/min |
| Channel commenters | `messages.getDiscussionMessage` → `messages.getReplies` | anyone unless `join_to_send` | The main person-data path for a broadcast channel | https://core.telegram.org/api/discussion |
| Recent commenters (free) | `messageReplies.recent_repliers` | anyone reading the post | A handful of peers; zero extra requests; poll over time | https://core.telegram.org/constructor/messageReplies |
| Reactors + reaction timestamps | `messages.getMessageReactionsList` → `messagePeerReaction{peer_id, date, reaction}` | member, **groups only** | **403 `BROADCAST_FORBIDDEN` on channels** — *"cannot be fetched to prevent deanonymization"* | https://core.telegram.org/method/messages.getMessageReactionsList |
| Poll voters | `messages.getPollVotes` | member, **groups only**, **and you must vote first** | 403 `BROADCAST_FORBIDDEN`; 403 `POLL_VOTE_REQUIRED` | https://core.telegram.org/method/messages.getPollVotes |
| Join/leave events **with timestamps** | `messageActionChatAddUser`, `…JoinedByLink{inviter_id}`, `…JoinedByRequest`, `…ChatDeleteUser` | anyone who can read history | Supergroups only; frequently hidden or cleared by admins | https://core.telegram.org/schema |
| Who read a message, and when | `messages.getMessageReadParticipants` → `readParticipantDate{user_id, date}` | member | Groups ≤ **100** members, messages < **7 days** old. Catches lurkers who never post | https://core.telegram.org/method/messages.getMessageReadParticipants |
| Live join/leave/promote stream | `updateChannelParticipant` | likely bot/admin (`qts`) | UNVERIFIED for plain user accounts | https://core.telegram.org/constructor/updateChannelParticipant |
| Boosters | `premium.getBoostsList` → `boost{user_id, date, expires, stars}` | **admin** | 400 `CHAT_ADMIN_REQUIRED` | https://core.telegram.org/method/premium.getBoostsList |
| Who joined via which invite link | `messages.getChatInviteImporters` → `chatInviteImporter{user_id, date, about, approved_by}` | **admin** | | https://core.telegram.org/method/messages.getChatInviteImporters |
| Join/leave/ban/promote history | `channels.getAdminLog` | **admin** | | https://core.telegram.org/method/channels.getAdminLog |
| Member sample from an invite link, without joining | `messages.checkChatInvite` → `chatInvite.participants` | anyone with the `t.me/+hash` | Also `participants_count`, title, photo | https://core.telegram.org/method/messages.checkChatInvite |
| Pending join requesters | `channelFull.recent_requesters`, `updatePendingJoinRequests` | admin | | https://core.telegram.org/constructor/channelFull |

*(Per-user profile fields, gifts/stories, contacts/privacy and rate-limit tables follow in their own sections.)*
# PART 1 — Participants (drafted, to be merged)

## 1.1 `channels.getParticipants` — the method

`channels.getParticipants#77ced9d0 channel:InputChannel filter:ChannelParticipantsFilter offset:int limit:int hash:long = channels.ChannelParticipants`
Source: https://core.telegram.org/method/channels.getParticipants

Returns `channels.channelParticipants{count:int, participants:Vector<ChannelParticipant>, chats:Vector<Chat>, users:Vector<User>}` or `channels.channelParticipantsNotModified` (when `hash` matches).

Documented errors (verbatim from the method page):

| Code | Type | Description |
|---|---|---|
| 400 | CHANNEL_INVALID | The provided channel is invalid. |
| 400 | CHANNEL_MONOFORUM_UNSUPPORTED | Monoforums lack this functionality. |
| 406 | CHANNEL_PRIVATE | You haven't joined this channel/supergroup. |
| 403 | CHAT_ADMIN_REQUIRED | You must be an admin in this chat to do this. |
| 400 | MSG_ID_INVALID | Invalid message ID provided. |

**Per-request page size is 200.** Not stated on core.telegram.org; it is the constant every client uses. Telethon: `_MAX_PARTICIPANTS_CHUNK_SIZE = 200` — https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/chats.py (line 14).

**`participants_count` is public to anyone**, even when the list is not: Telethon falls back to `channels.getFullChannel` → `full_chat.participants_count` when it cannot enumerate (chats.py `_ParticipantsIter._init`). Confirmed by `channelFull.participants_count` — https://core.telegram.org/constructor/channelFull

## 1.2 The filters

All from https://core.telegram.org/type/ChannelParticipantsFilter

| Constructor | Fields | Doc description |
|---|---|---|
| `channelParticipantsRecent` | — | "Fetch only recent participants" |
| `channelParticipantsAdmins` | — | "Fetch only admin participants" |
| `channelParticipantsKicked` | `q:string` | "Fetch only kicked participants" |
| `channelParticipantsBots` | — | "Fetch only bot participants" |
| `channelParticipantsBanned` | `q:string` | "Fetch only banned participants" |
| `channelParticipantsSearch` | `q:string` | "Query participants by name" |
| `channelParticipantsContacts` | `q:string` | "Fetch only participants that are also contacts" |
| `channelParticipantsMentions` | `q:flags.0?string`, `top_msg_id:flags.1?int` | see below |

**`channelParticipantsMentions` — exact doc text (high OSINT value):**
> "This filter is used when looking for supergroup members to mention. This filter will automatically remove anonymous admins, and **return even non-participant users that replied to a specific thread through the comment section of a channel**."
Source: https://core.telegram.org/constructor/channelParticipantsMentions

Note `channelParticipantsKicked` = *restricted* users; `channelParticipantsBanned` semantics are inverted relative to the names in Telethon's docs — Telethon warns `ChannelParticipantsBanned` returns *restricted* users and you want `ChannelParticipantsKicked` for actually-banned users. Source: Telethon `iter_participants` docstring, https://docs.telethon.dev/en/stable/modules/client.html

## 1.3 BROADCAST CHANNEL — what a non-admin subscriber can get

**Answer: nothing. Not the subscribers, not even the admin list.**

Authoritative evidence from TDLib (Telegram's own official client library) source:

1. Members are *always* hidden in broadcast channels. `ChatManager::can_hide_channel_participants()`:
```cpp
if (get_channel_type(c) != ChannelType::Megagroup) {
  return Status::Error(400, "Group members are hidden by default in channels");
}
```
https://github.com/tdlib/td/blob/master/td/telegram/ChatManager.cpp

2. The admin list is explicitly refused for broadcast channels when you are not an admin. `DialogParticipantManager::reload_dialog_administrators()`:
```cpp
case DialogType::Channel: {
  auto channel_id = dialog_id.get_channel_id();
  if (td_->chat_manager_->is_broadcast_channel(channel_id) &&
      !td_->chat_manager_->get_channel_status(channel_id).is_administrator()) {
    return query_promise.set_error(400, "Administrator list is inaccessible");
  }
```
https://github.com/tdlib/td/blob/master/td/telegram/DialogParticipantManager.cpp

3. The gate flag is `channelFull.can_view_participants` (flags.3) — "Can we view the participant list?" — which TDLib maps straight to `can_get_participants`:
`auto can_get_participants = channel->can_view_participants_;` (ChatManager.cpp ~line 5928).
https://core.telegram.org/constructor/channelFull

**Practically:** for a broadcast channel where you are a plain subscriber, `can_view_participants` is false and `channels.getParticipants` returns **403 CHAT_ADMIN_REQUIRED** for every filter. TDLib does not even send the request for the admins filter. The only membership-adjacent number you get is `participants_count` (subscriber count), which is public.

CAVEAT / partial UNVERIFIED: I verified this from TDLib source + the documented `CHAT_ADMIN_REQUIRED` error, not by executing a live call against a broadcast channel. TDLib refusing client-side means the raw-MTProto response for `channelParticipantsAdmins` on a broadcast is inferred, not directly observed. Recommend an empirical smoke test before relying on it.

## 1.4 SUPERGROUP / megagroup — the limits

### The ~10,000 cap
Undocumented, server-side, intentional. Established in Telethon issue #573 (Jan 2018):
- stek29: *"Yup, there's a server side limit and Telethon can do nothing with it."*
- stek29: *"That limit is put intentionally, and there's no 'official' way to overcome it. Play with search queries :P"*
- faustow, after testing: *"the limit seems to be set per `client` and not per request nor search."*
- Lonami: *"You must remember the API is only there to serve official applications, and official applications for a normal usage don't need to show *all* members…"*
https://github.com/LonamiWebs/Telethon/issues/573

`hash` does not help — it only enables the `channelParticipantsNotModified` short-circuit (Lonami, same thread).

### The letter-prefix `Search` trick
Origin: Telethon issue #580 (Feb 2018), by CodeDem. Iterate `channelParticipantsSearch(q)` for q in 'a'..'z', union the results by user ID.
https://github.com/LonamiWebs/Telethon/issues/580

Reported yields (all from that thread, community-measured, 2018):
- CodeDem: 21,163 / 24,754 (~85%) on @whalepoolbtc; a rerun got 24,015 (~97%).
- Lonami: 24,444 / 24,760 on @whalepoolbtc; **93,296 / 100,000** on a 100k group, ~5 minutes.
- ericxor61a0c0d: ~99% on a 15k group by unioning the alphabet sweep **with** a plain `Recent`/empty-search pass — needed because the alphabet sweep misses CJK/Cyrillic/emoji names.
- CodeDem ran it without sleeps and hit no flood wait ("I think telegram does not restrict calls to the search queries") — 2018 observation, do not assume it holds now.

**Status today: the hack is dead in Telethon.** It was shipped as `aggressive=True`, then neutered. Telethon changelog, **v1.25.1 (2022-09-24)**:
> "`aggressive` in `client.iter_participants` now does nothing (it did not really work anymore anyway, and this should prevent other errors)."
https://docs.telethon.dev/en/stable/misc/changelog.html ; release date from https://pypi.org/pypi/telethon/json

Current docstring: *"aggressive (bool, optional): Does nothing. This is kept for backwards-compatibility. There have been several changes to Telegram's API that limits the amount of members that can be retrieved, and this was a hack that no longer works."*
https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/chats.py

Also, Lonami on offsets (2024-10-03, issue #4385): *"The library uses it. It used to work fine. Not anymore though. Nothing the library can do… I strongly believe this remains a restriction on Telegram's side."*
https://github.com/LonamiWebs/Telethon/issues/4385

**Design implication:** implement the alphabet/prefix sweep yourself (multi-charset, not just a–z) but treat it as best-effort with unknown and declining yield. Do not promise completeness. Deduplicate by user ID. Always union with a plain `Recent` pass.

### Hidden members ("Hide members", 2022)
- Toggle: `channels.toggleParticipantsHidden#6a6e7854 channel:InputChannel enabled:Bool = Updates` — "Hide or display the participants list in **a supergroup**." Supergroup-only; requires `hidden_members_group_size_min` participants; errors include `CHAT_ADMIN_REQUIRED`, `PARTICIPANTS_TOO_FEW`.
  https://core.telegram.org/method/channels.toggleParticipantsHidden
- Flag: `channelFull.participants_hidden` (flags2.2) — "Whether the participant list is hidden."
  https://core.telegram.org/constructor/channelFull
- `hidden_members_group_size_min` **= 100** (default). https://core.telegram.org/api/config

**What a non-admin actually receives when hidden — precise answer from TDLib:**
> `has_hidden_members_` — "True, if **non-administrators can receive only administrators and bots** using getSupergroupMembers or searchChatMembers."
https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1supergroup_full_info.html

So it is **not** an empty list (popular SEO blogs claim an empty array — that is wrong): you still get **admins + bots**, and `count` still reflects the true total. TDLib corroborates by treating `total_count` under a hidden+`Recent` query as the *administrator* count:
```cpp
int32 administrator_count =
    filter.is_administrators() || (filter.is_recent() && has_hidden_participants) ? total_count : -1;
```
https://github.com/tdlib/td/blob/master/td/telegram/DialogParticipantManager.cpp

Effective-hidden logic (admins bypass it):
```cpp
bool ChatManager::get_channel_effective_has_hidden_participants(...) {
  if (c == nullptr || c->is_monoforum) return true;
  if (get_channel_status(c).is_administrator()) return false;
  ...
  return channel_full->has_hidden_participants || !channel_full->can_get_participants;
}
```

## 1.5 `channels.getParticipant` (single user)

`channels.getParticipant channel:InputChannel participant:InputPeer = channels.ChannelParticipant`
https://core.telegram.org/method/channels.getParticipant
Errors: 403 CHAT_ADMIN_REQUIRED, 406 CHANNEL_PRIVATE, 400 USER_NOT_PARTICIPANT ("You're not a member of this supergroup/channel").

Useful as a **membership oracle**: given a candidate user (harvested from messages, reactions, etc.) confirm membership + fetch their join date, without enumerating.

**Notably, TDLib does NOT gate this one client-side.** `DialogParticipantManager::get_channel_participant()` (DPM.cpp ~1877) calls `GetChannelParticipantQuery` directly, with no `has_hidden_participants` check and no `is_broadcast_channel` check — in sharp contrast to `reload_dialog_administrators()`, which hard-refuses on broadcasts (§1.3). That asymmetry strongly suggests the single-participant lookup remains usable where bulk enumeration is not, i.e. **in a hidden-members supergroup you can still confirm and date specific users one at a time.**
UNVERIFIED whether the *server* also permits it on a broadcast channel for a non-admin — needs a smoke test. This is the highest-value single thing to verify empirically before building.

Design implication: pair this with the discovery vectors in §1.8 — harvest candidate IDs cheaply from messages/reactions/service messages, then use `getParticipant` to convert each candidate into a confirmed member **with a join date**. That reconstructs much of what the hidden list withholds, one request per user.

## 1.6 `ChannelParticipant*` field matrix — exact TL (layer 223)

From https://core.telegram.org/schema

```
channelParticipant#1bd54456        flags:# user_id:long date:int subscription_until_date:flags.0?int rank:flags.2?string
channelParticipantSelf#a9478a1a    flags:# via_request:flags.0?true user_id:long inviter_id:long date:int subscription_until_date:flags.1?int rank:flags.2?string
channelParticipantCreator#2fe601d3 flags:# user_id:long admin_rights:ChatAdminRights rank:flags.0?string
channelParticipantAdmin#34c3bb53   flags:# can_edit:flags.0?true self:flags.1?true user_id:long inviter_id:flags.1?long promoted_by:long date:int admin_rights:ChatAdminRights rank:flags.2?string
channelParticipantBanned#d5f0ad91  flags:# left:flags.0?true peer:Peer kicked_by:long date:int banned_rights:ChatBannedRights rank:flags.2?string
channelParticipantLeft#1b03f006    peer:Peer
```

| Field | Participant | Self | Creator | Admin | Banned | Left |
|---|---|---|---|---|---|---|
| `user_id` | yes | yes | yes | yes | — (`peer`) | — (`peer`) |
| `peer` | — | — | — | — | yes | yes |
| **`date`** | **yes** ("Date joined") | **yes** | **NO** | **yes** ("When did the user join") | yes (**ban date**) | **NO** |
| `inviter_id` | **NO** | yes | NO | **only when `self`** (shares flags.1) | NO | NO |
| `via_request` | NO | yes | NO | NO | NO | NO |
| `promoted_by` | NO | NO | NO | **yes** | NO | NO |
| `kicked_by` | NO | NO | NO | NO | **yes** | NO |
| `admin_rights` | NO | NO | yes | yes | NO | NO |
| `banned_rights` | NO | NO | NO | NO | yes | NO |
| `rank` | yes | yes | yes | yes | yes | NO |
| `subscription_until_date` | yes | yes | NO | NO | NO | NO |
| `can_edit` / `self` | NO | — | NO | yes | NO | NO |

Answering the caller's question directly — **join date (`date`) availability**:
- `channelParticipant` — **YES**, "Date joined". This is the big one: every ordinary member you can enumerate comes with a join timestamp.
- `channelParticipantSelf` — YES (plus `inviter_id`, plus `via_request` = joined via an approved join request).
- `channelParticipantAdmin` — YES, and the doc says it is the **join** date, not the promotion date ("When did the user join"). https://core.telegram.org/constructor/channelParticipantAdmin
- `channelParticipantCreator` — **NO date field at all.** You cannot get the creator's join date this way.
- `channelParticipantBanned` — YES but it is the **ban** date, not the join date.
- `channelParticipantLeft` — no fields but `peer`.

**`inviter_id` gotcha (precise):** on `channelParticipantAdmin`, `self:flags.1?true` and `inviter_id:flags.1?long` share bit 1. So `inviter_id` is only ever populated when the admin **is you**. You cannot learn who invited another admin. `channelParticipant` (ordinary members) has no `inviter_id` at all — inviter attribution for third parties is only available to admins via the admin log / invite importers.

Other fields:
- `rank` — the custom admin title ("tag"), https://core.telegram.org/api/rank. Present on ordinary participants too. Often leaks org structure / real names.
- `subscription_until_date` — "expiration date of the current Telegram Star subscription period for the specified participant" — reveals **paying subscribers** of a paid channel/group and when their subscription lapses.
- `channelParticipantBanned.peer` is a `Peer`, not a user — channels can be banned in channels (layer 126).

## 1.7 Basic groups (`chat`, not `channel`) — full list always available

`messages.getFullChat` → `chatFull.participants` is a `chatParticipants` with the **complete** list; docs: "clients are **always** supposed to fetch and store the full participant list of basic groups… because it is the only source of data for information that must be visible in the message UI".
https://core.telegram.org/api/channel

```
chatParticipant#38e79fde       flags:# user_id:long inviter_id:long date:int rank:flags.0?string
chatParticipantAdmin#360d5d2   flags:# user_id:long inviter_id:long date:int rank:flags.0?string
chatParticipantCreator#e1f867b8 flags:# user_id:long rank:flags.0?string
chatParticipantsForbidden#8763d3e1 ...
```
**In a basic group you get `inviter_id` AND `date` for every member** — strictly more than a supergroup gives you. Max 200 members. If the group has migrated, `channelFull.migrated_from_chat_id` points back. `chatParticipantsForbidden` = you lack access (Telethon handles this by returning total 0).

# Member discovery when the list is hidden — ranked by yield

*(This is the dedicated section the brief asked for. It sits here because it depends on the participant-API facts above.)*

Ranking assumes the realistic worst case established in §1.11: a broadcast channel, or a supergroup with "Hide members" on, where `channels.getParticipants` yields only admins and bots.

| # | Vector | Method / field | Access | Yield & caveats |
|---|---|---|---|---|
| 1 | **Message authors** | `messages.getHistory` / `messages.search(from_id=…)`; `message.from_id` | any member (any reader for public groups) | Highest yield by far in an active supergroup. Only covers people who *spoke*. Users arrive as `min` constructors → see §9. Anonymous admins appear as the channel peer, not a user. |
| 2 | **`channelParticipantsMentions`** | `channels.getParticipants` with this filter, optionally `top_msg_id` | member | Officially documented to return **non-participant users who commented on a channel post thread** — reaches beyond the member list by design. Strips anonymous admins. Best single "hidden members" trick that is still officially sanctioned. |
| 3 | **Linked discussion group** | `messages.getDiscussionMessage` → `messages.getReplies` on the linked supergroup | any (unless `join_to_send`) | For a broadcast channel this is *the* way to get real users. Channel posts are auto-forwarded into the linked group and comments are a thread there. Commenting does not require joining unless the admin set `channels.toggleJoinToSend` (then non-members get `CHAT_GUEST_SEND_FORBIDDEN`). https://core.telegram.org/api/discussion |
| 4 | **Reactors** | `messages.getMessageReactionsList` → `messagePeerReaction{peer_id, date, reaction}` | member (**groups only**) | **Blocked on broadcast channels: 403 BROADCAST_FORBIDDEN — "Channel poll voters and reactions cannot be fetched to prevent deanonymization."** In supergroups it is excellent: gives peer + **exact reaction timestamp**. Paid reactions (`messageReactor`) carry an `anonymous` flag. |
| 5 | **Join/leave service messages** | `messageActionChatAddUser{users}`, `messageActionChatJoinedByLink{inviter_id}`, `messageActionChatJoinedByRequest`, `messageActionChatDeleteUser{user_id}` | any history reader | **Join events with message timestamps** — a longitudinal membership timeline, including people who have since left. Only present in supergroups where join notices are shown, and they are frequently hidden/cleared by admins. `messageActionChatJoinedByLink` gives the *inviter* — social graph. |
| 6 | **`recent_repliers`** | `messageReplies{comments, replies, recent_repliers:Vector<Peer>, channel_id, max_id}` | anyone who can read the post | Free with every channel post — "the last few comment posters… to show a small list of commenter profile pictures". Tiny (a handful) and only when `comments` is set, but zero extra requests. Poll it over time to accumulate. https://core.telegram.org/constructor/messageReplies |
| 7 | **Poll voters** | `messages.getPollVotes` | member, **and you must vote first** | Non-anonymous polls only. **403 BROADCAST_FORBIDDEN** on channels. **403 POLL_VOTE_REQUIRED — "Cast a vote in the poll before calling this method."** Requires an active, visible footprint — flag this. |
| 8 | **Read receipts** | `messages.getMessageReadParticipants` → `readParticipantDate{user_id, date}` | member | Groups with ≤ `chat_read_mark_size_threshold` (**100**) members, messages younger than `chat_read_mark_expire_period` (**604800s = 7 days**). Errors `CHAT_TOO_BIG`, `MSG_TOO_OLD`. Tiny groups only — but gives **who read what, when**, i.e. lurkers who never post. |
| 9 | **Forward attributions / mentions** | `message.fwd_from`, `messageEntityMentionName` | any reader | Cheap side-channel. `fwd_from` is suppressed if the origin user enabled the forwards privacy rule (then only `private_forward_name`). |
| 10 | **Admin log** | `channels.getAdminLog` (join/leave/invite/ban/promote/demote filters) | **admin only** | If you have admin: definitive join/leave/promote history. |
| 11 | **Invite importers** | `messages.getChatInviteImporters` → `chatInviteImporter{user_id, date, about, approved_by, requested, via_chatlist}` | **admin only** | Who joined via which link, when, who approved. |
| 12 | **Boosters** | `premium.getBoostsList` → `boost{user_id, date, expires, multiplier, stars, giveaway_msg_id}` | **admin only** (400 CHAT_ADMIN_REQUIRED) | Not available to non-admins. |
| 13 | **Story viewers** | `stories.getStoryViewsList` | **story author only** | Not a channel-member vector for a third party. |
| 14 | **`recent_requesters`** | `channelFull.recent_requesters` (flags.28) | admin (in practice) | IDs of users who recently requested to join. |

**Two vectors deserve emphasis because they are officially sanctioned rather than hacks:** `channelParticipantsMentions` (#2) is *documented* to return non-participants who commented, and the linked discussion group (#3) is the designed mechanism for channel comments. Neither is a workaround Telegram is likely to close.

**Three are admin-only and should be detected-and-skipped, not attempted:** boosts, invite importers, admin log. Calling them without rights burns requests and returns `CHAT_ADMIN_REQUIRED`.

# Back to participant-API mechanics

## 1.9 `min` users and the `FromMessage` trick

Users harvested from message history typically arrive as **`min`** constructors: reduced fields, and an `access_hash` that is not generally usable.
> "In some situations user and channel constructors have reduced set of fields present (although `id` is always there) and `min` flag set." … "The `access_hash` value of a `min` constructor is only suitable to use in certain conditions."
https://core.telegram.org/api/min

Resolution path:
1. If a non-`min` object for that ID is already in your local peer DB, **merge** — "the client must first check if user or chat object without `min` flag is already present in the local peer database. If it is present, then the client should merge the remote and the local object."
2. Otherwise, remember *where you saw them* (peer + message ID) and construct:
   - `inputPeerUserFromMessage{peer, msg_id, user_id}` / `inputUserFromMessage{peer, msg_id, user_id}`
   - `inputPeerChannelFromMessage` / `inputChannelFromMessage`
   These carry the message context so the server can re-derive a valid access hash. This is what makes `users.getFullUser` work on a user you only ever saw in a group.

**Design implication:** the scraper's user store must persist `(user_id → (source_peer, source_msg_id))` for every `min` user, or those users become unresolvable later. This is the single most common reason naive Telegram scrapers fail on "users I saw in a group".

## 1.10 Config constants worth hard-coding as defaults

From https://core.telegram.org/api/config
- `hidden_members_group_size_min` = 100
- `chat_read_mark_size_threshold` = 100
- `chat_read_mark_expire_period` = 604800 (7 days)
- `pm_read_date_expire_period` = 604800

TDLib client-side caching thresholds (not server caps, but indicative): megagroup 975, broadcast 195 (`max_participant_count` in DialogParticipantManager.cpp).

TDLib corroborates the 200 page size in `on_update_dialog_online_member_count_timeout`, which for a non-hidden megagroup under 195 members calls
`get_channel_participants(channel_id, ChannelParticipantFilter::recent(), string(), 0, 200, 200, Auto())`
and otherwise falls back to `messages.getOnlines` — **a cheap online-count signal that works even when the member list is hidden.**

`messages.getOnlines peer:InputPeer = ChatOnlines` → `chatOnlines#f041e250 onlines:int`. Errors are only `CHANNEL_PRIVATE`, `CHAT_ID_INVALID`, `PEER_ID_INVALID` — **no `CHAT_ADMIN_REQUIRED`**, so any member can call it, and TDLib invokes it precisely on the hidden-participants branch. Polled on a schedule it yields an **activity/timezone curve for a group whose membership you cannot enumerate at all** — aggregate, non-identifying, and cheap. https://core.telegram.org/method/messages.getOnlines

## 1.11 Current-state reality check on enumeration yield (2022–2024)

The 2018 "10,000" figure is obsolete and optimistic. More recent maintainer-confirmed data points:

- **2022-03-25, issue #3781** — "get_participants for channel only returns 200 members", reported by an account that *was an admin* of the channels. Lonami: *"This is a Telegram limitation the library cannot fix."* https://github.com/LonamiWebs/Telethon/issues/3781
- **2023-05-29, issue #4117** — Lonami: *"If you haven't updated Telethon, and you haven't changed your code, then the most likely thing is the Telegram server itself has changed. Telegram doesn't really want you to fetch all members. Telethon is not going to implement hacky workarounds — spammers could abuse those and they tend to break."* https://github.com/LonamiWebs/Telethon/issues/4117
- **2024-06-01, issue #4385** (Telethon 1.35.1) — a megagroup with **78,000 members** returned **12 users** from `get_participants()`, with limit/filter/aggressive variations all making no difference. Lonami confirmed it as an API limitation. https://github.com/LonamiWebs/Telethon/issues/4385

That last figure is almost certainly the hidden-members path (12 ≈ admins + bots), consistent with TDLib's `has_hidden_members` semantics in §1.4 — **but the reporter never checked the flag, so the attribution is my inference, not established fact.**

**Plan for a yield between "everything" and "a dozen".** Read `channelFull.participants_hidden` and `can_view_participants` first and branch: if either indicates hidden, skip the enumeration sweep entirely and go straight to the discovery vectors in §1.8 rather than burning requests and flood budget on a list that will return admins and bots.
# PART 2 — `users.getFullUser`, `user`, `userFull` (from agent A)

Schema baseline: **Layer 223**.

## 0. The docs pages disagree with themselves on CRCs

The headline TL line on `/constructor/user` and `/constructor/userFull` is **stale relative to the parameter tables on the same pages**:

| | Stale headline | Authoritative (agrees across /api/privacy, /api/profile, tl.telethon.dev) |
|---|---|---|
| `user` | `user#31774388` … ends at `bot_forum_can_manage_topics:flags2.17` | same CRC **+ `bot_can_manage_bots:flags2.18?true bot_guestchat:flags2.19?true`** |
| `userFull` | `userFull#a02bc13e` … | **`userFull#06cbe645`** … **+ `unofficial_security_risk:flags2.26?true`, `bot_manager_id:flags2.25?long`** |

**Do not hardcode `userFull#a02bc13e`.** Telethon v1 is pinned at **layer 222**.
https://core.telegram.org/api/privacy , https://core.telegram.org/api/profile , https://tl.telethon.dev/constructors/user_full.html

Legend: `PUBLIC` anyone who can resolve the user · `PRIV` privacy-gated · `CONTACT` contacts-only by default · `SELF` only meaningful for the logged-in account · `BOT` bot-only · `REL` describes *your* relationship (server-computed per-caller) · `MIN` subject to `min`-constructor suppression

## 1. The method

```
users.getFullUser#b60f5918 id:InputUser = users.UserFull;
users.userFull#3b6d152e full_user:UserFull chats:Vector<Chat> users:Vector<User> = users.UserFull;
```
https://core.telegram.org/method/users.getFullUser

- **Both users and bots.** Requires a valid `InputUser` (non-`min` access_hash, or `inputUserFromMessage`).
- Errors: `USER_ID_INVALID`, `CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`, `USERNAME_OCCUPIED`.
- **There is no privacy-denial error.** Privacy is enforced by *omitting flags*, not by failing the call. **The single most important design fact: absence of a field is the signal.**
- Returns `UserFull` **plus** `users:Vector<User>` for the target — always parse both; `user` and `userFull` carry disjoint data.
- Cache invalidation triggers: changes to `deleted`, `bot`, `premium`, `bot_info_version`, `usernames`, and `username` (when `bot_can_edit`).

## 2. `user` constructor — every field

### 2a. First flag word
| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `self` | flags.10?true | currently logged in user | SELF | |
| `contact` | flags.11?true | is a contact | REL | "do not apply changes to this field if the `min` flag is set" |
| `mutual_contact` | flags.12?true | mutual contact | REL | `min`-suppressed |
| `deleted` | flags.13?true | account was deleted | PUBLIC | loses names/username/photo |
| `bot` | flags.14?true | is a bot | PUBLIC | |
| `bot_chat_history` | flags.15?true | "Can the bot see all messages in groups?" | BOT | i.e. privacy mode disabled |
| `bot_nochats` | flags.16?true | "Can the bot be added to groups?" | BOT | **name is inverted vs meaning** — read the doc text |
| `verified` | flags.17?true | verified | PUBLIC | official check, distinct from `bot_verification_icon` |
| `restricted` | flags.18?true | access restricted, reason in `restriction_reason` | PUBLIC | shares bit with `restriction_reason` |
| `min` | flags.20?true | see /api/min | — | field-suppression master switch |
| `bot_inline_geo` | flags.21?true | bot can request geolocation inline | BOT | |
| `support` | flags.23?true | official support user | PUBLIC | **impersonation detection** |
| `scam` | flags.24?true | may be a scam user | PUBLIC | server-assigned |
| `apply_min_photo` | flags.25?true | min `photo` may update local DB | — | see §8.7 |
| `fake` | flags.26?true | reported by many as fake/scam | PUBLIC | distinct from `scam` |
| `bot_attach_menu` | flags.27?true | offers attachment-menu web app | BOT | |
| `premium` | flags.28?true | Telegram Premium user | PUBLIC | |
| `attach_menu_enabled` | flags.29?true | **we** installed this bot's attach menu | REL | `min`-suppressed |

### 2b. Second flag word
| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `bot_can_edit` | flags2.1?true | **we own this bot** and can edit its profile | REL/SELF | `min`-suppressed. Strong ownership signal |
| `close_friend` | flags2.2?true | **we** marked them a close friend | REL | `min`-suppressed; contacts only |
| `stories_hidden` | flags2.3?true | **we** hid their active stories | REL | `min`-suppressed |
| `stories_unavailable` | flags2.4?true | no stories visible | PRIV | **ambiguous** — none posted *or* story privacy excludes you |
| `contact_require_premium` | flags2.10?true | non-contacts need Premium to write | PRIV | **raw flag** — set even for mutual contacts you *can* write to. Use `userFull.contact_require_premium` or `users.getRequirementsToContact` |
| `bot_business` | flags2.11?true | Business bot | BOT | |
| `bot_has_main_app` | flags2.13?true | has a Main Mini App | BOT | |
| `bot_forum_view` | flags2.16?true | supports bot forum topics | BOT | |
| `bot_forum_can_manage_topics` | flags2.17?true | users may create/manage topics in the private chat | BOT | |
| `bot_can_manage_bots` | flags2.18?true | manager bot | BOT | not in stale headline TL |
| `bot_guestchat` | flags2.19?true | invocable as a guest in chats | BOT | not in stale headline TL |

### 2c. Value fields
| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `id` | long | user ID | PUBLIC | always present even in `min`; 64-bit since layer 133 |
| `access_hash` | flags.0?long | per-account access hash | PUBLIC/MIN | **`min_access_hash` algorithm — §8.7.** May be absent |
| `first_name` | flags.1?string | | PUBLIC/MIN | absent on deleted accounts |
| `last_name` | flags.2?string | | PUBLIC/MIN | often genuinely absent |
| `username` | flags.3?string | "Main active username" | PUBLIC/MIN | only the *primary active* one |
| `usernames` | flags2.0?Vector<Username> | additional usernames | PUBLIC/MIN | `username#b4073647 flags:# editable:flags.0?true active:flags.1?true username:string`. **`editable` = "wasn't bought on fragment"** ⇒ **`editable` UNSET ⇒ Fragment/collectible username**, a monetary-linkage signal. https://core.telegram.org/constructor/username |
| `phone` | flags.4?string | phone number | **PRIV** | §8.2. **May be an empty string in `min` constructors** — test non-empty, never flag presence |
| `photo` | flags.5?UserProfilePhoto | avatar | PRIV/MIN | `userProfilePhoto#82d1f706 flags:# has_video:flags.0?true personal:flags.2?true photo_id:long stripped_thumb:flags.1?bytes dc_id:int`. `has_video`=animated; **`personal`= "only visible to us (set using `photos.uploadContactProfilePhoto`)"**; `stripped_thumb`=inline low-res, free, no download. https://core.telegram.org/constructor/userProfilePhoto |
| `status` | flags.6?UserStatus | online status | **PRIV** | §6 |
| `bot_info_version` | flags.14?int | increments when bot_info changes | BOT | monotonic change-detector; shares bit 14 with `bot` |
| `restriction_reason` | flags.18?Vector<RestrictionReason> | | PUBLIC | `restrictionReason#d072acb4 platform:string reason:string text:string`. **Geo/platform-specific — you only see reasons applicable to your declared platform/region.** https://core.telegram.org/constructor/restrictionReason |
| `bot_inline_placeholder` | flags.19?string | | BOT | |
| `lang_code` | flags.22?string | user's language | **BOT-ONLY** | **§8.1 — definitively bots-only** |
| `emoji_status` | flags.30?EmojiStatus | | PUBLIC | `emojiStatusEmpty` / `emojiStatus{document_id, until}` / **`emojiStatusCollectible{collectible_id, document_id, title, slug, pattern_document_id, center_color, edge_color, pattern_color, text_color, until}`** — the collectible variant leaks a **named gift + slug**, a wealth/identity fingerprint. https://core.telegram.org/type/EmojiStatus |
| `stories_max_id` | flags2.5?RecentStory | active-story summary | PRIV | `recentStory#711d692d flags:# live:flags.0?true max_id:flags.1?int`. **`live` = currently broadcasting a live story.** Fully suppressed if `min` |
| `color` | flags2.8?PeerColor | accent color (messages) | PUBLIC | `peerColor#b54b5acf flags:# color:flags.0?int background_emoji_id:flags.1?long`. Palette IDs 0–6 built-in; **higher IDs are Premium-only ⇒ non-default color implies Premium**. https://core.telegram.org/api/colors |
| `profile_color` | flags2.9?PeerColor | profile-page color | PUBLIC | separate palette (`help.getPeerProfileColors`) |
| `bot_active_users` | flags2.12?int | bot MAU | BOT/PUBLIC | **the only participants_count-equivalent on `user`**. Absent for small bots ⇒ absence is itself a size signal |
| `bot_verification_icon` | flags2.14?long | | PUBLIC | **third-party** verification (a bot vouching), NOT official `verified` |
| `send_paid_messages_stars` | flags2.15?long | paid messages enabled | PRIV | raw; see `userFull` for resolved amount |

## 3. `userFull` constructor — every field

### 3a. Flags
| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `blocked` | flags.0?true | **you** blocked them | REL/SELF | your state |
| `phone_calls_available` | flags.4?true | can make VoIP calls | PUBLIC | capability, not permission |
| `phone_calls_private` | flags.5?true | their privacy allows you to call | PRIV/REL | **name misleading** — set means privacy *permits* |
| `can_pin_message` | flags.7?true | only for chat with yourself | SELF | Saved Messages marker |
| `has_scheduled` | flags.12?true | scheduled messages available | REL/SELF | |
| `video_calls_available` | flags.13?true | | PUBLIC | |
| `voice_messages_forbidden` | flags.20?true | disallows voice messages in PM | PRIV | `privacyKeyVoiceMessages`. **Premium-only setting ⇒ implies Premium** |
| `translations_disabled` | flags.23?true | | SELF | your client preference |
| `stories_pinned_available` | flags.26?true | has pinned stories | PRIV | gate for `stories.getPinnedStories` |
| `blocked_my_stories_from` | flags.27?true | **we** blocked them from our stories | REL/SELF | |
| `wallpaper_overridden` | flags.28?true | they chose a custom wallpaper for us | REL | |
| `contact_require_premium` | flags.29?true | we cannot write: need Premium | PRIV/REL | **fully resolved** — "not just a copy of `user.contact_require_premium`" |
| `read_dates_private` | flags.30?true | cannot fetch exact read date | PRIV | target set `hide_read_marks`; `messages.getOutboxReadDate` → `USER_PRIVACY_RESTRICTED` |
| `sponsored_enabled` | flags2.7?true | ads re-enabled | **SELF** | "only accessible to the currently logged-in user" |
| `can_view_revenue` | flags2.9?true | can view bot ad revenue | BOT/SELF | |
| `bot_can_manage_emoji_status` | flags2.10?true | | BOT/REL | |
| `display_gifts_button` | flags2.16?true | | REL | requires **mutual** opt-in |
| `noforwards_my_enabled` | flags2.23?true | content protection enabled **by us** | SELF | |
| `noforwards_peer_enabled` | flags2.24?true | content protection enabled **by this user** | PUBLIC-ish | **target's opsec posture** |
| `unofficial_security_risk` | flags2.26?true | **"this user uses an unofficial Telegram client, and messages sent to them may be less secure"** | PUBLIC | **High-value.** TDLib: `userFullInfo.uses_unofficial_app`. Not in stale headline TL. https://core.telegram.org/api/profile |

### 3b. Value fields
| Field | Type | Meaning | Access | Caveats |
|---|---|---|---|---|
| `id` | long | | PUBLIC | non-optional |
| `about` | flags.1?string | bio | **PRIV** | `privacyKeyAbout`. Absent ⇒ no bio *or* you're excluded |
| `settings` | PeerSettings | | mixed | **non-optional — richest OSINT surface, see Part 3 §D** |
| `personal_photo` | flags.21?Photo | **a photo YOU set for THEM** | **SELF** | via `photos.uploadContactProfilePhoto` (contacts only). **Zero information about the target.** TDLib: "isn't returned in the list of user photos" |
| `profile_photo` | flags.2?Photo | their real avatar | PRIV | absent when `privacyKeyProfilePhoto` excludes you |
| `fallback_photo` | flags.22?Photo | "displayed if no photo is present in `profile_photo` or `personal_photo`, **due to privacy settings**" | PRIV | **`fallback_photo` present + `profile_photo` absent ⇒ positive proof you are privacy-excluded** |
| `notify_settings` | PeerNotifySettings | | SELF | non-optional |
| `bot_info` | flags.3?BotInfo | | BOT/PUBLIC | `botInfo#4d8a0299` → user_id, description, description_photo, description_document, commands, menu_button, **`privacy_policy_url:flags.7?string`** (often a real external domain — **pivotable**), app_settings, verifier_settings, has_preview_medias |
| `pinned_msg_id` | flags.6?int | | REL | in *your* chat with them |
| `common_chats_count` | int | chats in common | REL | **non-optional — always present.** Enumerate via `messages.getCommonChats`. Counts only chats *you* are also in |
| `folder_id` | flags.11?int | | SELF | archive = 1 |
| `ttl_period` | flags.14?int | auto-delete timer (s) | REL | |
| `theme` | flags.15?ChatTheme | | REL | **⚠ replaced the old `theme_emoticon:flags.15?string`.** Now `chatTheme#c3dffc04 emoticon:string` or `chatThemeUniqueGift#3458f9c8 gift:StarGift theme_settings:Vector<ThemeSettings>` — emoticon is nested. https://core.telegram.org/type/ChatTheme |
| `private_forward_name` | flags.16?string | anonymized forward name | PRIV | set ⇒ `privacyKeyForwards` restricts you ⇒ forwards unlinkable. **Its presence is the signal** |
| `bot_group_admin_rights` | flags.17?ChatAdminRights | suggested admin rights for groups | BOT | reveals intended privilege footprint |
| `bot_broadcast_admin_rights` | flags.18?ChatAdminRights | same, channels | BOT | |
| ~~`premium_gifts`~~ | ~~flags.19?Vector<PremiumGiftOption>~~ | — | — | **REMOVED.** Bit 19 unused in layer 222/223. Superseded by `stargifts_count`/`disallowed_gifts`. **Do not implement** |
| `wallpaper` | flags.24?WallPaper | | REL | |
| `stories` | flags.25?PeerStories | active stories | PRIV | `peerStories#9a35e999 flags:# peer:Peer max_read_id:flags.0?int stories:Vector<StoryItem>` — **inlines actual StoryItems, no extra call** |
| `business_work_hours` | flags2.0?BusinessWorkHours | | **PUBLIC** | `businessWorkHours#8c92b098 flags:# open_now:flags.0?true timezone_id:string weekly_open:Vector<BusinessWeeklyOpen>`. **`timezone_id` leaks the target's timezone**; `weekly_open` up to 28 intervals in minutes-of-week; **`open_now` server-computed** ⇒ live open/closed |
| `business_location` | flags2.1?BusinessLocation | | **PUBLIC** | `businessLocation#ac5c1af7 flags:# geo_point:flags.0?GeoPoint address:string`. **`address` mandatory (≤96 chars), `geo_point` optional lat/lon. Highest-value geolocation field for users in the whole API** |
| `business_greeting_message` | flags2.2?BusinessGreetingMessage | | **SELF-ONLY** | docs: "can be fetched **by the current user**". Do not expect for third parties |
| `business_away_message` | flags2.3?BusinessAwayMessage | | **SELF-ONLY** | same |
| `business_intro` | flags2.4?BusinessIntro | | **PUBLIC** | `businessIntro#5a0a066d flags:# title:string description:string sticker:flags.0?Document`. "shown to **new users that don't have a private chat with us**" ⇒ genuinely third-party visible. ⚠ /api/business calls it `userFull.intro` — doc typo |
| `birthday` | flags2.5?Birthday | | **PRIV** | `birthday#6c8e1e06 flags:# day:int month:int year:flags.0?int`. §8.3 |
| `personal_channel_id` | flags2.6?long | associated personal channel | PUBLIC | **Major pivot: user → channel identity link.** Only public channels the user admins are eligible ⇒ **implies the target is an admin of that public channel** |
| `personal_channel_message` | flags2.6?int | latest previewed message | PUBLIC | shares bit 6 |
| `stargifts_count` | flags2.8?int | gifts **chosen to display** | PUBLIC | displayed count, not total received |
| `starref_program` | flags2.11?StarRefProgram | | BOT | |
| `bot_verification` | flags2.12?BotVerification | | PUBLIC | third-party verification detail |
| `send_paid_messages_stars` | flags2.14?long | Stars to message them | PRIV/REL | **`>0` = you must pay N Stars; `=0` = they require payment generally but *you specifically* are exempt** — a relationship signal |
| `disallowed_gifts` | flags2.15?DisallowedGiftsSettings | | PUBLIC | |
| `stars_rating` | flags2.17?StarsRating | star rating badge | PUBLIC | from total successful Stars transaction volume — **a spend-magnitude proxy** |
| `stars_my_pending_rating` | flags2.18?StarsRating | | **SELF** | "only visible for ourselves" |
| `stars_my_pending_rating_date` | flags2.18?int | | **SELF** | shares bit 18 |
| `main_tab` | flags2.20?ProfileTab | | PUBLIC | |
| `saved_music` | flags2.21?Document | first song on music tab | PRIV | `privacyKeySavedMusic` |
| `note` | flags2.22?TextWithEntities | "a private note for this contact, **only visible to us**" | **SELF** | *your* note. Never their data |
| `bot_manager_id` | flags2.25?long | manager of a managed bot | BOT | **Bot → operator identity pivot.** Not in stale headline TL |

## 5. Profile photos

```
photos.getUserPhotos#91cd32a8 user_id:InputUser offset:int max_id:long limit:int = photos.Photos;
photos.photos#8dca6aa5 photos:Vector<Photo> users:Vector<User>          // complete list
photos.photosSlice#15051f54 count:int photos:Vector<Photo> users:Vector<User>  // paginated, has total
```
https://core.telegram.org/method/photos.getUserPhotos

- **Both users and bots.** Errors: `USER_ID_INVALID`, `MAX_ID_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`.
- **limit max = 100**, confirmed twice: TDLib `//@limit The maximum number of photos to be returned; up to 100`; Telethon `_MAX_PROFILE_PHOTO_CHUNK_SIZE = 100`.
- `max_id` with the `limit=1, offset=-1` idiom is for **refetching file references**.
- Order: "from the most recent photo to the oldest."
- Telethon `_ProfilePhotoIter` dispatches on entity type — **USER → `photos.GetUserPhotosRequest`**; **channels/chats → `messages.SearchRequest` with `filter=InputMessagesFilterChatPhotos`**, extracting `MessageActionChatEditPhoto.photo`.

**`photo.date` IS present:**
```
photo#fb197a65 flags:# has_stickers:flags.0?true id:long access_hash:long file_reference:bytes
  date:int sizes:Vector<PhotoSize> video_sizes:flags.1?Vector<VideoSize> dc_id:int = Photo;
```
`date:int` = "Date of upload". TDLib equivalent `chatPhoto.added_date`. `video_sizes` present ⇒ animated avatar.
⚠ UNVERIFIED whether *upload* date and *set-as-avatar* date coincide when re-using an existing photo.

**Full history? Yes, with two documented exclusions.** TDLib `getUserProfilePhotos`: "Returns the profile photos of a user. **Personal and public photo aren't returned**". So the list is the target's **own uploaded avatar history**, oldest retained through current, excluding photos *you* set for them and their fallback photo. **Old avatars the user replaced but never deleted remain retrievable — the core OSINT value.**

**Non-contacts / privacy — UNVERIFIED.** No primary source states the interaction with `privacyKeyProfilePhoto`; the method has no privacy-specific error. Strong inference: a restricted caller gets an empty/short list and must fall back to `userFull.fallback_photo`. Determine empirically.

**Downloading:** `inputPeerPhotoFileLocation#37257e99 flags:# big:flags.0?true peer:InputPeer photo_id:long`. **This is the one location a `min` access hash still works.** For zero-cost triage use the inline `userProfilePhoto.stripped_thumb`.

## 6. `UserStatus`

```
userStatusEmpty#9d05049
userStatusOnline#edb93949 expires:int
userStatusOffline#8c703f was_online:int
userStatusRecently#7b197dc8 flags:# by_me:flags.0?true
userStatusLastWeek#541a1d1a flags:# by_me:flags.0?true
userStatusLastMonth#65899777 flags:# by_me:flags.0?true
```
https://core.telegram.org/type/UserStatus

| Constructor | Payload | OSINT value |
|---|---|---|
| `userStatusEmpty` | — | No signal. Also the bucket for bots and for privacy-hidden-with-no-approximation |
| `userStatusOnline` | `expires` | **Exact.** unix ts when the online state lapses ⇒ live presence |
| `userStatusOffline` | `was_online` | **Exact last-seen unix timestamp** — maximum fidelity; enables sleep-cycle/timezone inference by longitudinal polling |
| `userStatusRecently` | `by_me` | Coarse (≈ within 2–3 days; exact width UNVERIFIED) |
| `userStatusLastWeek` | `by_me` | Coarse |
| `userStatusLastMonth` | `by_me` | Coarse |

**`by_me` — verbatim from /constructor/userStatusRecently:**
> "If set, the exact user status of this user is actually available to us, but to view it we must first purchase a Premium subscription, or allow this user to see our exact last online status."

TDLib names it `by_my_privacy_settings`: "Exact user's status is hidden because the current user enabled `userPrivacySettingShowStatus` privacy setting for the user and has no Telegram Premium".

**Operational consequence — a tooling requirement:**
- `by_me = true` ⇒ **the target is NOT hiding from you.** *Your own* collector's privacy config is degrading the data. Fix with allow-all `privacyKeyStatusTimestamp` or Premium.
- `by_me = false` on a coarse bucket ⇒ the target's privacy genuinely restricts you.
- A tool that ignores `by_me` will silently mis-attribute self-inflicted blindness to target opsec.

## 7. `messages.getCommonChats`

```
messages.getCommonChats#e40ca104 user_id:InputUser max_id:long limit:int = messages.Chats;
```
**"Only users can use this method"** — no bots. Errors `USER_ID_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`. Pairs with `userFull.common_chats_count` (total up front). **Inherently relative to the collector** — "common" means chats *you* are also in.

## 8. The critical details, resolved

### 8.1 `lang_code` — BOT-ONLY, confirmed
TDLib `td_api.tl` states it outright: `//@language_code IETF language tag of the user's language; only available to bots`.
Corroborating: **Telegram Desktop never reads it** — the only `user`-context occurrences are placeholders `MTPstring(), // lang_code` in `main_account.cpp` and `data_session.cpp`. TDLib guards on emptiness (`UserManager.cpp:3389`), implying the normal case is empty. Bot API exposes it as `User.language_code`.

**Verdict: a user-account client should treat `user.lang_code` as permanently absent. Do not build any feature on it.** There is no MTProto method to obtain another user's language.

### 8.2 `phone` — when non-null for a third party
Gated by `privacyKeyPhoneNumber`. Non-null when: it's your own account; the target's rules include you (client default is "My Contacts" — UNVERIFIED at API-doc level); or they shared it via `contacts.acceptContact`/`addContact` with `add_phone_privacy_exception`.

Two hard caveats:
1. **`phone` can be present as an empty string.** The `min_access_hash` algorithm branches on "The `phone` flag is set and the associated phone number string is non-empty" — `flags.4` set with `""` is a real wire state. **Always test non-empty.**
2. **Being a contact does not imply having the phone.** /api/contacts: "Telegram users may also be added to the contact list (**even if we do not have access to their phone number!**)".

### 8.3 `birthday` — `inputPrivacyKeyBirthday`
`/api/profile`: "will be displayed to the users specified in the privacy settings… (**only contacts by default**)". `year` is optional — expect day/month only in most cases. Range `0 <= years <= 150` (`BIRTHDAY_INVALID`). Field is on **`userFull.birthday`** (/api/profile says "user.birthday" — doc typo).

**Two side-channel oracles:**
- `contacts.getBirthdays#daeda864` returns all *contacts* with birthdays within ±1 day of today.
- **`users.suggestBirthday#fc533372 id:InputUser birthday:Birthday` is a privacy oracle**: succeeds if the target has no birthday set *or* one you can't see; returns **`400 BIRTHDAY_ALREADY`** if they have one visible to you — distinguishing "not set" from "set but hidden". **NOT passive**: success sends a `messageActionSuggestBirthday` service message and a push notification to the target. **Never invoke from a collection account.**

### 8.5 `profile_photo` vs `personal_photo` vs `fallback_photo`
| | Who set it | Who sees it | OSINT meaning |
|---|---|---|---|
| `profile_photo` | The target | Anyone their `privacyKeyProfilePhoto` permits | **Their real current avatar** |
| `personal_photo` | **YOU** (contacts only) | **Only you** | **Zero information about the target.** A tool that ingests it as target data is simply wrong — and it silently shadows `profile_photo` in the UI |
| `fallback_photo` | The target, `photos.updateProfilePhoto` + `fallback` | Users **excluded** by their photo privacy | **`fallback_photo` set + `profile_photo` absent = positive machine-readable proof your collector is privacy-excluded** — flag it, don't record "no photo" |

`userProfilePhoto.personal:flags.2?true` also appears on the lightweight `user.photo`, so you can detect the personal case without `getFullUser`.

### 8.7 `min` users, `apply_min_photo`, access_hash validity
https://core.telegram.org/api/min — "Usually min constructors are encountered in messages inside of groups or channels."

**The `min_access_hash` algorithm (verbatim from /constructor/user):**
> Set `min_access_hash` to **true** if `min` is set AND (the `phone` flag is not set OR the `phone` flag is set and the associated phone number string is non-empty); false otherwise.
> **If the final merged object stored to the database has `min_access_hash` set to true, the related `access_hash` is only suitable to use in `inputPeerPhotoFileLocation`, to directly download the profile pictures of users, everywhere else a `inputPeer*FromMessage` constructor will have to be generated.** Bots can also use min access hashes in some conditions, by passing `0` instead.

- **Store provenance.** "the client must store the context (similar to file references) in which the user/channel was seen" — the `(chat, msg_id)` pair. **Your peer table needs `(peer_id, seen_in_chat, seen_in_msg_id)`, not just `(peer_id, access_hash)`.**
- Additional pivots: "`user_id` can also be set to the IDs of users met in the `fwd_header` (messages forwarded from a user can be used to interact with the original sender, **if they don't have privacy settings for forwards enabled**). Users mentioned via `messageEntityMentionName` in a message can also be used."
- **Suppressed when `min` is set** (never overwrite cached full data): `contact`, `mutual_contact`, `attach_menu_enabled`, `bot_can_edit`, `close_friend`, `stories_hidden`, `stories_max_id`.
- **Applied only if** `min` unset OR cached entry is itself `min`: `first_name`, `last_name`, `username`, `phone`, `usernames`.
- **`apply_min_photo`** — `photo` applies if `min` unset, OR (`min` set AND (`apply_min_photo` set OR cached is `min`)). The one explicit escape hatch for refreshing an avatar from a min constructor.
- **`status`** applies if `min` unset, OR (`min` set AND (cached is `min` OR cached equals `userStatusEmpty`)).

## 9. Cross-cutting design notes

1. **Absence is the signal, not an error.** Model every optional field as tri-state: `present` / `absent-because-not-set` / `absent-because-privacy`. Disambiguators: `fallback_photo` set ⇒ photo privacy-excluded; `by_me` set ⇒ *your* status privacy is the cause; `private_forward_name` set ⇒ forward privacy on; `send_paid_messages_stars == 0` ⇒ they charge others but exempt you.
2. **Calibrate your collector or you will misread targets.** `by_me` and `common_chats_count` are both functions of *your* account's configuration.
3. **Two-tier collection.** `users.getUsers#d91a548 id:Vector<InputUser>` is **batched** — use it for cheap bulk triage (names, username, premium/verified/scam/fake/deleted, emoji_status, color, stories_max_id, stripped thumb). Reserve the **unbatched** `users.getFullUser` (one call per user) for targets that pass triage.
4. **Passively capture `peerSettings` on inbound first contact** — the only documented window for `registration_month`/`phone_country`/`name_change_date`/`photo_change_date`. Event-driven, architecturally distinct from on-demand lookup.
5. **Never call `users.suggestBirthday`** from a collection account — it notifies the target.
6. **Highest-yield third-party-visible fields, ranked:** `business_location.geo_point`/`address` → `business_work_hours.timezone_id` → `personal_channel_id` → `peerSettings.registration_month`/`phone_country` → profile-photo history with dates → `usernames` with `editable` unset → `unofficial_security_risk` → `bot_manager_id`.
7. **Schema churn is real.** `theme_emoticon` → `theme:ChatTheme`; `premium_gifts` removed; `unofficial_security_risk`/`bot_manager_id`/`bot_guestchat` added. Pin a layer; validate against the pinned TL, not the docs' headline lines.

### UNVERIFIED in this section
- Whether `photos.getUserPhotos` returns empty vs partial for privacy-restricted non-contacts.
- FLOOD_WAIT thresholds for bulk profile-photo or `getFullUser` scraping.
- Exact time widths of `userStatusRecently` / `LastWeek` / `LastMonth`.
- Whether `photo.date` is upload time or set-as-avatar time when re-using a photo.
- Telegram's *default* privacy value for `privacyKeyPhoneNumber`.
- Whether `peerSettings` provenance fields populate via `messages.getPeerSettings` for an arbitrary already-known peer.
# PART 3 — Contacts, privacy gating, account age (from agent C)

## HEADLINE: There IS an official registration-date field (narrowly gated)

```
peerSettings#f47741f7 flags:# ... registration_month:flags.15?string phone_country:flags.16?string
                              name_change_date:flags.17?int photo_change_date:flags.18?int = PeerSettings;
```
https://core.telegram.org/constructor/peerSettings

From https://core.telegram.org/api/action-bar, verbatim:
> "**When a user contacts you for the first time**, the `registration_month`, `phone_country`, `name_change_date` and `photo_change_date` flags will be set in their `peerSettings` constructor: these fields should be used to generate an info box… The above information can come in handy for users to detect and block spam or recently hijacked accounts."

Corroborated in TDLib:
```
accountInfo registration_month:int32 registration_year:int32 phone_number_country_code:string
            last_name_change_date:int32 last_photo_change_date:int32 = AccountInfo;
chatActionBarReportAddBlock can_unarchive:Bool account_info:accountInfo = ChatActionBar;
```
https://github.com/tdlib/td/blob/master/td/generate/scheme/td_api.tl

**Access bound is the whole story:** in TDLib `accountInfo` is reachable ONLY via `chatActionBarReportAddBlock` — it is NOT a field of `userFullInfo`. So it is available only for a non-contact who messaged YOU first, while the report/add/block action bar is live. Month granularity. Not queryable for arbitrary targets.

Nothing equivalent exists in `user` or `userFull`. `userFull.birthday` is self-reported and privacy-gated, not a registration date.

UNVERIFIED: which client release shipped the user-facing box.

## A) Resolution & search methods

| Data item | Method / constructor | Access | Caveats | Source |
|---|---|---|---|---|
| username → peer | `contacts.resolveUsername#725afbbc flags:# username:string referer:flags.0?string` → `contacts.ResolvedPeer` | users + bots | `referer` is a star-referral ID, not an HTTP referer. Errors `USERNAME_INVALID`, `USERNAME_NOT_OCCUPIED`, `STARREF_EXPIRED` | https://core.telegram.org/method/contacts.resolveUsername |
| phone → peer | `contacts.resolvePhone#8af94344 phone:string` | users only | **Doc-stated rate limit: "at most 1 call every 3 seconds"**. `PHONE_NOT_OCCUPIED` is ambiguous (see below) | https://core.telegram.org/method/contacts.resolvePhone , https://core.telegram.org/api/contacts |
| global username/name search | `contacts.search#11f812d8 q:string limit:int` → `contacts.found#b3134d9d my_results:Vector<Peer> results:Vector<Peer> chats:Vector<Chat> users:Vector<User>` | users only | Errors `QUERY_TOO_SHORT`, `SEARCH_QUERY_EMPTY`. Doc contradiction on scope — see below | https://core.telegram.org/method/contacts.search |
| sponsored search injections | `contacts.getSponsoredPeers#b6c8c393 q:string` | users only | **Ad content rendered between `my_results` and `results` — filter it out or it poisons the dataset** | https://core.telegram.org/api/contacts |
| own contact list | `contacts.getContacts#5dd69e12 hash:long` | **self only** | — | https://core.telegram.org/method/contacts.getContacts |
| contact IDs | `contacts.getContactIDs#7adc669d hash:long` | self only | "0 is returned for contacts [that] do not have an associated Telegram account **or have hidden their account using privacy settings**" | https://core.telegram.org/api/contacts |
| full saved phonebook | `contacts.getSaved#82f1e39f` | self only | **Requires a takeout session** | https://core.telegram.org/api/contacts |
| **phone → user (bulk)** | `contacts.importContacts#2c800be5` | users only | ⚠️ highly sensitive — see below | https://core.telegram.org/method/contacts.importContacts |
| contact presence poll | `contacts.getStatuses#c4a353ee` → `Vector<contactStatus{user_id, status}>` | self only, no params | Cheap bulk presence poll over the whole contact list | https://core.telegram.org/method/contacts.getStatuses |
| people nearby | `contacts.getLocated#d348bc44` | users only | ⚠️ client feature removed Sept 2024 — see below | https://core.telegram.org/method/contacts.getLocated , https://core.telegram.org/api/nearby |
| user IDs → objects | `users.getUsers#d91a548 id:Vector<InputUser>` | users + bots | Needs valid `access_hash` (or `inputUserFromMessage`). No documented max batch size | https://core.telegram.org/method/users.getUsers |
| username existence probe | `account.checkUsername#2714d86c` | users only | `USERNAME_PURCHASE_AVAILABLE` ⇒ **listed on fragment.com** | https://core.telegram.org/method/account.checkUsername |
| own behavioral graph | `contacts.getTopPeers#973478b6` | **self only** | correspondents / bots_pm / phone_calls / forward_users / groups / channels … | https://core.telegram.org/method/contacts.getTopPeers |
| `t.me/+hash` invite | `messages.checkChatInvite#3eadb1bb hash:string` → `ChatInvite` | users only | **Leaks a member sample without joining** — see below | https://core.telegram.org/method/messages.checkChatInvite |
| can I message X? | `users.getRequirementsToContact#d89a83a3` | users | `requirementToContactEmpty` / `…Premium` / `…PaidMessages stars_amount:long` | https://core.telegram.org/api/privacy |

### `contacts.importContacts` — the phone-enumeration primitive
```
inputPhoneContact#6a1dc4be flags:# client_id:long phone:string first_name:string last_name:string note:flags.0?TextWithEntities
importedContact#c13e3c50 user_id:long client_id:long
popularContact#5ce14175 client_id:long importers:int
contacts.importedContacts#77d01c3b imported:Vector<ImportedContact> popular_invites:Vector<PopularContact> retry_contacts:Vector<long> users:Vector<User>
```
- Reveals a user for an arbitrary phone, gated by ONE key: "according to the user's privacy settings (specifically, the `inputPrivacyKeyAddedByPhone` privacy key), not all contacts which have an associated Telegram account may be returned here." — https://core.telegram.org/api/contacts
- `users` returns **full `User` objects** (name, username, photo, status, premium, access_hash), not just IDs.
- `retry_contacts` — "could not be imported due to a **server-side system limitation** and have to be reimported with another call." The soft throttle; partial success is normal.
- `popular_invites` — **underrated signal**: `popularContact.importers` = how many Telegram users have this phone in their address book. A crude notability metric, returned even for numbers with no Telegram account.
- **Not passive**: "saves a full list on the server, adds already registered contacts to the contact list". Mutates account state and is visible to the other side. Needs `contacts.deleteContacts` + `contacts.deleteByPhones` + `contacts.resetSaved` cleanup.
- Correct key is `inputPrivacyKeyAddedByPhone#d1219bdd` ("Whether people can add you to their contact list by your phone number"). `inputPrivacyKeyPhoneNumber#0352dafa` is a *different* key (whether the number is displayed).

**No published numeric limits.** Only official rate statements: the `resolvePhone` 3s debounce, `retry_contacts`, and generic `420 FLOOD_WAIT_%d` / `FLOOD_PREMIUM_WAIT_%d`. Any "N contacts/day" figure from growth-hacking blogs is UNVERIFIED — do not hardcode.

### `contacts.resolvePhone` — negatives are uninterpretable
> "If there is no Telegram account associated with the specified phone number, `PHONE_NOT_OCCUPIED` will be returned. **The same error will be returned if the target account's privacy settings** (specifically, the `inputPrivacyKeyAddedByPhone` privacy key) **prevent phone number lookups of their account.**" — https://core.telegram.org/api/contacts

⇒ Report `UNKNOWN`, never "no account". Model tri-state HIT / UNKNOWN / INVALID_NUMBER.

### `contacts.search` — doc contradiction (UNVERIFIED scope)
- Method page: returns users by **username substring**, and **excludes your own contacts**.
- /api/contacts: "Use `contacts.search` to search **within the contact list**."
These disagree. The `my_results` vs `results` split suggests it does both. Determine empirically.

### `messages.checkChatInvite` — member sample without joining
`chatInvite#5c9d3702` returns `title`, `about`, `photo`, `participants_count:int`, and **`participants:flags.4?Vector<User>`** — a sample of actual members — plus `request_needed`, `verified`, `scam`, `fake`. Variants `chatInviteAlready`, `chatInvitePeek#61695cb0 chat:Chat expires:int` (temporary read access). Errors `INVITE_HASH_INVALID/EMPTY/EXPIRED`, `CHANNEL_PRIVATE`.

### `contacts.getLocated` — People Nearby is dead
Durov announced removal **6 Sept 2024** ("had issues with bots and scammers"), replaced by Businesses Nearby — https://www.irishtimes.com/world/europe/2024/09/06/telegram-founder-pavel-durov-says-arrest-in-france-is-misguided/ ; Telegram staff confirmed same day on the official tracker: "This feature has been removed" — https://bugs.telegram.org/c/43188
BUT the method is still present at Layer 223 with no deprecation notice and /api/nearby still documents it. Whether the server still returns populated results is **UNVERIFIED**. Treat as dead; probe once, degrade gracefully.
Two properties if ever live: (1) you can fetch nearby users **without publishing your own location** by omitting `self_expires` — asymmetric read; (2) publishing requires a profile photo (`USERPIC_UPLOAD_REQUIRED`, `USERPIC_PRIVACY_REQUIRED`).

### Every phone↔user path
| Direction | Mechanism | Gate |
|---|---|---|
| phone → user | `contacts.resolvePhone` | `added_by_phone` |
| phone → user (bulk) | `contacts.importContacts` | `added_by_phone` |
| user → phone | `user.phone` (flags.4) | `privacyKeyPhoneNumber` |
| user → phone | `contacts.getSaved` (takeout) | own phonebook only |
| user → phone **country** | `peerSettings.phone_country` | action-bar gated |
| user → phone (consensual) | `contacts.acceptContact#f831a20f` / `contacts.addContact` w/ `add_phone_privacy_exception` | mutual action |

## B) Privacy keys — full list (verified against the master schema)

**Documentation bug:** the schema block on /api/privacy **omits `privacyKeyBirthday`**, which exists in the master schema as `privacyKeyBirthday#2000a518`. Generate your enum from https://core.telegram.org/schema , not that page.

| InputPrivacyKey | PrivacyKey | Gates | What a third party observes when blocked |
|---|---|---|---|
| `inputPrivacyKeyStatusTimestamp#4f96cb18` | `privacyKeyStatusTimestamp#bc2eab30` | exact last-online | Coarse bucket `userStatusRecently` / `LastWeek` / `LastMonth`. **Not** `userStatusEmpty` (that means never set) |
| `inputPrivacyKeyChatInvite#bdfb0426` | `privacyKeyChatInvite#500e6dfa` | being added to groups | invite fails (exact error UNVERIFIED) |
| `inputPrivacyKeyPhoneCall#fabadc5f` | `privacyKeyPhoneCall#3d662b7b` | accepting calls | `userFull.phone_calls_available` unset |
| `inputPrivacyKeyPhoneP2P#db9e70d2` | `privacyKeyPhoneP2P#39491cc8` | P2P vs relayed VoIP | `userFull.phone_calls_private` — **leaks that they force relay**, a mild opsec tell |
| `inputPrivacyKeyForwards#a4dd4c08` | `privacyKeyForwards#69ec56a3` | forward attribution | `userFull.private_forward_name` = "Anonymized text… instead of the user's name on forwarded messages"; `fwd_from` has no linkable `from_id` |
| `inputPrivacyKeyProfilePhoto#5719bacc` | `privacyKeyProfilePhoto#96151fed` | profile picture | `userFull.fallback_photo` = "displayed if no photo is present… **due to privacy settings**" |
| `inputPrivacyKeyPhoneNumber#0352dafa` | `privacyKeyPhoneNumber#d19ae46d` | displaying the number | `user.phone` absent |
| `inputPrivacyKeyAddedByPhone#d1219bdd` | `privacyKeyAddedByPhone#42ffd42b` | **discovery by phone** | `resolvePhone`→`PHONE_NOT_OCCUPIED`; omitted from `importContacts.imported`; `getContactIDs`→0 |
| `inputPrivacyKeyVoiceMessages#aee69d68` | `privacyKeyVoiceMessages#0697f414` | voice/video notes | `userFull.voice_messages_forbidden` |
| `inputPrivacyKeyAbout#3823cc40` | `privacyKeyAbout#a486b761` | bio | `userFull.about` absent |
| `inputPrivacyKeyBirthday#d65a11cc` | `privacyKeyBirthday#2000a518` | birthday | `userFull.birthday` absent |
| `inputPrivacyKeyStarGiftsAutoSave#e1732341` | `privacyKeyStarGiftsAutoSave#2ca4fdf8` | auto-display gifts | `userFull.stargifts_count` suppressed |
| `inputPrivacyKeyNoPaidMessages#bdc597b4` | `privacyKeyNoPaidMessages#17d348d2` | who may message free | `user.send_paid_messages_stars`, `peerSettings.charge_paid_message_stars` |
| `inputPrivacyKeySavedMusic#4dbe9226` | `privacyKeySavedMusic#ff7a571b` | songs pinned to profile | `userFull.saved_music` absent |

Rules (12): `AllowContacts` / `AllowAll` / `AllowUsers` / `DisallowContacts` / `DisallowAll` / `DisallowUsers` / `AllowChatParticipants` / `DisallowChatParticipants` / `AllowCloseFriends` (stories only) / `AllowPremium` (only for `chat_invite`) / `AllowBots` / `DisallowBots`. https://core.telegram.org/type/PrivacyRule

`account.getPrivacy` / `account.setPrivacy` are **self-only** — you can read your own rules, never a target's. https://core.telegram.org/method/account.getPrivacy

### Last-seen reciprocity — VERIFIED, with a Premium loophole
Verbatim, https://core.telegram.org/constructor/userStatusRecently:
> "Note that if we decide to hide our exact last online timestamp to someone… **and we do not have a Premium subscription**, we won't be able to see the exact last online timestamp of those users… If those users do share their exact online status with us, but we can't see it due to the reason mentioned above, the `by_me` flag of `userStatusRecently`, `userStatusLastWeek`, `userStatusLastMonth` will be set."

Three consequences:
1. **Premium buys out of reciprocity** — a Premium collection account hides its own last-seen AND still reads exact timestamps. Highest-leverage account config for presence work.
2. **`by_me` is an oracle** — when set, the target DOES share exact status and you are the one blocked. Distinguishes "target is private" from "you are throttled". Record as a distinct state.
3. Non-Premium + hidden own status ⇒ presence data silently degraded to 3 buckets. FAQ: "Last seen recently — covers anything between 1 second and 2-3 days" — https://telegram.org/faq

### Premium / paid inbound gating
`globalPrivacySettings#fe41b34f` (`account.getGlobalPrivacySettings#eb2b4cf6`):
- `new_noncontact_peers_require_premium` → surfaces as `user.contact_require_premium`. Messaging yields `403 PRIVACY_PREMIUM_REQUIRED`. **Caveat from /api/privacy: the `user.` flag is a coarse hint — "a mutual contact will have this flag set even if we can still write to them". Authoritative answer is `userFull.contact_require_premium` or `users.getRequirementsToContact`.**
- `noncontact_peers_paid_stars` → `user.send_paid_messages_stars`.
- `hide_read_marks` → `userFull.read_dates_private`; `messages.getOutboxReadDate` then returns `USER_PRIVACY_RESTRICTED`.

## C) Account age from user ID — community interpolation

@creationdatebot bio: "shows the **approximate** creation date for any account in telegram", 40,761 monthly users, redirecting to @devctl — https://t.me/creationdatebot

Verified repos (each actually fetched):
| Repo | Lang | Stars | Dataset |
|---|---|---|---|
| https://github.com/Jobians/telegram-id-age | JS (npm `telegram-id-age`) | 1 | **`dataset.json`: 212 anchor pairs**, `{"id":"0","date":"2013-08-14"}` … `{"id":"8559682245","date":"2025-11-11"}`. Most usable published anchor set found |
| https://github.com/bisnuray/telegram-account-age | Python | 1 | "using User ID interpolation… based on known ID-timestamp mappings" |
| https://github.com/karipov/creationDate | Python | — | **`src/data/dates.json.example` is 0 bytes — dataset NOT published** |

UNVERIFIED (surfaced but not fetched): `SantiiRepair/tdage`, `TheSmartDevs/SmartUserInfo`.

The ecosystem is thin — 1-star hobby repos, not maintained references. **Build and version your own anchor set.**

Accuracy limits — four, be honest about all:
1. **Sparse anchors.** 212 anchors over 12 years, unevenly spaced in ID space. The commonly cited ±2–3 months is **UNVERIFIED folklore** — derive a confidence interval from local anchor density, never emit a bare date.
2. **IDs are roughly, not strictly, monotonic.** Allocation is sharded across DCs and has been rebased. Use isotonic regression / PAVA, not naive linear interpolation.
3. **32→64-bit migration.** Layer **133 – "64-bit IDs for User/Chat"** changed `user.id` from `int` to `long`. Anchor sets predating layer 133 may carry truncated/wrapped IDs — real corruption risk when merging old CSVs.
4. **The "~5 billion max" community claim is WRONG.** Official: "**User IDs in the MTProto API range from 1 to `0xffffffffff` (inclusive)**" = 2⁴⁰−1 = 1,099,511,627,775 — https://core.telegram.org/api/bots/ids . Actually-issued IDs were ~8.56×10⁹ by Nov 2025. Validate against 2⁴⁰−1, extrapolate against observed issuance.

## D) `peerSettings` as an OSINT surface

`messages.getPeerSettings#efd9a6a2 peer:InputPeer` — users only. Also embedded free in `userFull.settings` and pushed via `updatePeerSettings#6a7e7366`. https://core.telegram.org/method/messages.getPeerSettings

| Field | Signal |
|---|---|
| `report_spam` / `add_contact` / `block_contact` | Relationship state; all three set ⇒ stranger who just messaged you — exactly the state where `registration_month` is populated |
| **`geo_distance:flags.6?int`** | ⚠️ "Distance in meters between us and this peer" — see below |
| `registration_month` / `phone_country` / `name_change_date` / `photo_change_date` | The account-provenance block |
| `request_chat_title:flags.9?string` / `request_chat_date:flags.9?int` | This peer is an **admin of a chat you requested to join**, with title + request timestamp — links a person to an org and dates the approach |
| `business_bot_id:flags.13?long` / `business_bot_manage_url` | Chat automated by a business bot — infrastructure fingerprint |
| `charge_paid_message_stars:flags.14?long` | Star price to message them |
| `share_contact` / `need_contacts_exception` | Whether they added you *without* a phone number |
| `autoarchived` | You were auto-archived by their privacy settings |

### `geo_distance` — a real leak, but NOT a free oracle
From https://core.telegram.org/api/action-bar, verbatim:
> "if the `peerSettings.geo_distance` flag is set, the bar should also display the distance between us and the user, **indicating that the user found us by invoking `contacts.getLocated`, because we are currently advertising our location with the same method**."

Precondition: it appears only when **YOU are actively publishing your own location**. Metres precision (trivially multilaterable), but requires your own location to be public — so it is an exposure risk to your collection accounts as much as a capability. Combined with People Nearby's removal, treat as legacy/defensive, not a collection primitive.
