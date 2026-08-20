# Prior Art: Open-Source Telegram OSINT / Scraping / Archiving Tools

**Research date: 2026-08-20.** All star counts, commit dates, and layer numbers were
verified directly against the GitHub / Codeberg APIs, PyPI JSON API, or
`core.telegram.org` on that date unless explicitly marked **UNVERIFIED**.

**Purpose:** inform the design of a local CLI that, given a Telegram channel, extracts
everything obtainable about it and dumps it locally. Three questions: (a) what do the
best tools collect, so we can be a superset; (b) what architecture/storage/rate-limit
lessons can we steal; (c) what pitfalls (bans, breakage) must we design around.

---

## 0. Headline findings (read this if you read nothing else)

1. **The Python MTProto ecosystem reorganised in 2026 and most "known good" answers are
   now stale.** `LonamiWebs/Telethon` on GitHub is **archived**; its final commit
   (2026-02-21) is literally titled *"Migrate off GitHub"* and points to
   <https://codeberg.org/Lonami/Telethon>. Telethon is *not* dead — it is actively
   developed on Codeberg (last commit 2026-08-19, TL **layer 228**) and still ships to
   PyPI as `Telethon` 1.44.0 (2026-06-15). Any tool, blog post, or LLM that says
   "Telethon is abandoned because the GitHub repo is archived" is wrong.
2. **`pyrogram/pyrogram` is genuinely dead** (archived, last push 2024-12-23, stuck at
   **layer 158**). The living fork is **Kurigram** (`KurimuzonAkuma/pyrogram`), at layer
   228 and committing daily. `pyrofork` is a distant second (layer 220, commits stopped
   2025-12-10).
3. **GramJS is archived too** (2026-07-14) with an explicit deprecation notice pointing
   at **`sanyok12345/teleproto`** (layer 228, MIT, active).
4. **A channel's subscriber list is not obtainable.** `channels.getParticipants` returns
   `CHAT_ADMIN_REQUIRED` for broadcast channels — this is a server-side rule, not a
   library limitation. No tool in this survey bypasses it, and any tool claiming to is
   either scraping a *supergroup* or reconstructing members from message senders. This
   materially constrains the product brief.
5. **Nearly every tool in the survey is a "messages + media" dumper.** The genuinely
   valuable OSINT surface that layer 228 exposes — edits, deletions, reaction lists,
   discussion-thread comments, similar-channel graph, boosts, stories, profile-photo
   history, admin-log, forward graph — is collected by almost nobody, and by no single
   tool. That gap is the opportunity.

---

## 1. The capability frontier: what the API actually exposes (layer 228)

This section is the yardstick for the feature matrix. It was derived by reading the
live TL schema (`td/generate/scheme/telegram_api.tl` from `tdlib/td` @ master, layer
228, 3113 lines, fetched 2026-08-20) and the machine-readable RPC error database at
<https://core.telegram.org/api/errors.json> (which reports `"layer": 227`; the public
docs site trails tdlib by a layer or two — `config.json` reports 225).

`errors.json` is the authoritative way to answer "does this need admin?", because it
maps every error to the exact list of methods that can emit it.

### 1.1 Obtainable by any logged-in account (no admin, no Premium)

| Method | Yields |
|---|---|
| `contacts.resolveUsername` | username → channel id + access_hash |
| `channels.getFullChannel` | `about`, `participants_count`, `admins_count`, `kicked_count`, `banned_count`, `online_count`, `linked_chat_id` (discussion group), `location`, `slowmode_seconds`, `stats_dc`, `pts`, `boosts_applied`, `boosts_unrestrict`, `available_min_id`, `migrated_from_chat_id`, `stargifts_count`, plus the flags `participants_hidden`, `can_view_participants`, `can_view_stats`, `antispam`, `restricted_sponsored`, `paid_media_allowed` |
| `channel#` (from `Chat`) | `date` = **channel creation timestamp**, `usernames:Vector<Username>` (multi-username), `restriction_reason:Vector<RestrictionReason>` (**which platforms/countries the channel is censored in**), `scam`/`fake`/`verified`/`gigagroup`/`noforwards` flags, `level` (boost level), `signature_profiles`, `linked_monoforum_id` |
| `messages.getHistory` | full message history: text, entities, `views`, `forwards`, `edit_date`, `post_author`, `from_rank`, `grouped_id` (albums), `reactions` (aggregate counts + `recent_reactions`), `replies` (comment count + `recent_repliers`), `fwd_from`, `restriction_reason`, `factcheck`, `from_boosts_applied`, `paid_message_stars`, `ttl_period` |
| `channels.getMessages` | fetch explicit id vectors; **returns `messageEmpty` for deleted ids** → passive deletion detection |
| `updates.getChannelDifference` | incremental sync from a stored `pts`: `updateNewChannelMessage`, **`updateEditChannelMessage`**, **`updateDeleteChannelMessages`**, `updateChannelMessageViews`, `updateChannelMessageForwards`, `updateMessageReactions` |
| `messages.getDiscussionMessage` + `messages.getReplies` | **comment threads** under channel posts (via the linked discussion supergroup) |
| `channels.getChannelRecommendations` | **similar channels** graph (capped by `recommended_channels_limit_default`; Premium gets `_premium`, a documented 2× limit) |
| `premium.getBoostsStatus` | boost level / count / next-level thresholds — **no admin error listed**, works on any peer |
| `stories.getPeerStories`, `stories.getStoriesArchive` | channel stories |
| `photos.getUserPhotos` | **profile photo history** for users |
| `messages.getSearchCounters` | per-`MessagesFilter` counts (photos/videos/links/docs/voice/gifs) — a whole media inventory in one cheap call |
| `messages.getSearchResultsCalendar` | per-period message-count histogram — posting-cadence profile without downloading the history |
| `messages.search` (per peer) | server-side filtering by `from_id`, date range, media filter |
| `messages.getSponsoredMessages` | ads currently served inside the channel |
| `users.getFullUser` | per-user: `about`, `birthday`, `personal_channel_id`, `business_location`, `business_work_hours`, `common_chats_count`, `stargifts_count`, `personal_photo`/`profile_photo`/`fallback_photo` |
| `t.me/s/<channel>` (no auth at all) | see §3.7 |

### 1.2 Admin-only (`CHAT_ADMIN_REQUIRED` per `errors.json`)

- `channels.getParticipants`, `channels.getParticipant` — **the member list**
- `channels.getAdminLog` — 52 distinct `ChannelAdminLogEventAction` types (title/photo/username changes, joins, bans, promotions, deletions, edits)
- `stats.getBroadcastStats` — views/shares/followers graphs, top hours, languages
- `stats.getMessagePublicForwards` — **who publicly reposted a given post** (outbound forward graph)
- `premium.getBoostsList` — the identities of boosters

### 1.3 Otherwise gated

- `messages.getMessageReactionsList` → `403 BROADCAST_FORBIDDEN`. **You cannot enumerate who reacted to a broadcast-channel post.** You get aggregate `ReactionCount` and a short `recent_reactions` vector inside the message, and nothing more.
- `messages.getPollVotes` → `403 BROADCAST_FORBIDDEN` and `403 POLL_VOTE_REQUIRED` (you must have voted).
- `channels.searchPosts` (global public-post / hashtag search) → `403 PREMIUM_ACCOUNT_REQUIRED`, plus `420 FROZEN_METHOD_INVALID` and a metered `SearchPostsFlood` object with an `allow_paid_stars` parameter — Telegram now **monetises** global post search.
- `account.initTakeoutSession` + `invokeWithTakeout` (<https://core.telegram.org/api/takeout>) — the *sanctioned* bulk export path with relaxed limits, but it only covers dialogs the account is a member of, requires `messages.getSplitRanges` pagination, and `TAKEOUT_INIT_DELAY_%d` **notifies every device on the account**. Loud, but ban-safe.

### 1.4 Hard ceilings

- `channels.getParticipants`: **200 per request** (`_MAX_PARTICIPANTS_CHUNK_SIZE = 200` in Telethon `client/chats.py:14`).
- `channels.getAdminLog`: **100 per request** (`_MAX_ADMIN_LOG_CHUNK_SIZE = 100`, same file, line 15).
- A widely repeated "~10,000 participant hard cap on the offset" is **UNVERIFIED** — I found no statement of it in `core.telegram.org` docs or the TL schema. Treat it as folklore worth empirically re-testing rather than a documented constant. What *is* documented is `hidden_members_group_size_min` (the supergroup size above which admins may hide the member list) in `/api/config.json`.
- `updates.getChannelDifference`: the server-side message box retains a bounded window of `pts` events. Requesting a `pts < (latestPts - size)` returns `updates.channelDifferenceTooLong`, and the client must re-fetch state and backfill gaps with `channels.getMessages` (<https://core.telegram.org/api/updates>). **Incremental sync degrades to a rescan if you fall too far behind.**

---

## 2. Tool-by-tool survey

### 2.A OSINT-branded investigation tools

#### Telepathy → `prose-intelligence-ltd/Telepathy-Community`
<https://github.com/prose-intelligence-ltd/Telepathy-Community> — **1,233★**, MIT, forks 161,
46 open issues. `jordanwildon/Telepathy` **301-redirects** here; the `jordanwildon` GitHub
user now **404s**. Owner is Prose Intelligence Ltd, which sells a commercial "Telepathy Pro".
PyPI: `telepathy` 2.3.4.

- **Library:** Telethon (user session) + `requests`/BeautifulSoup against `t.me/<channel>`.
  Note `requirements.txt` pins `Telethon==1.25.2` while `setup.py` pins `1.36.0` — the two
  install paths install different cores (issue #91).
- **Collects:** messages ✅, `edit_date` ✅, media ✅, reactions ✅ (~17 named emoji + total),
  comments/replies ✅ (`iter_messages(reply_to=…)`), **forward graph ✅** (Gephi-ready
  edgelist), participants ✅ but **hard-capped at 5,000** and groups-only, per-user profiles ✅,
  polls ❌, admin list ❌, join dates ❌, profile-photo history ❌, similar channels ❌,
  boosts ❌, stories ❌, deletions ❌. Channel stats: computes its *own* engagement rate;
  never calls `GetBroadcastStats`.
- **Storage:** CSV (`sep=";"`), optional JSON/JSONL. No SQLite output. **No incremental
  support** — every run re-reads full history.
- **Rate limiting: none.** Zero `FloodWaitError` references in the codebase; two
  `time.sleep(0.5)` calls; many bare `except: pass` (one swallows `KeyboardInterrupt`).
  Issue #90 documents that flood waits are swallowed so **a partial archive reports as
  complete** — the single most important cautionary lesson in this survey.
- **Status: broken and unmaintained.** Last `src/` change 2024-07-12 (2026 commits are CI /
  dependabot). The subagent verified by `ast.parse` that `src/telepathy/telepathy.py` on
  `main` raises `SyntaxError: expected an indented block after class definition on line
  1743`, **and that the published PyPI 2.3.4 sdist fails identically** — `pip install
  telepathy` ships a non-importable package. A stale `build/lib/` copy masks this.
- **Ethics:** harvests member phone numbers where exposed; `-l` uses
  `contacts.GetLocatedRequest` (People Nearby), which Telegram **removed as a consumer
  feature in Sept 2024**, so that module is very likely dead in practice.

#### tosint — `drego85/tosint`
<https://github.com/drego85/tosint> — **844★**, GPL-3.0, 0 open issues, pushed 2026-07-29,
actively maintained.

- **Library: Bot API over plain HTTPS** (+ **Pyrofork** for optional downloads). Different
  threat model: **you must already possess a bot token**; it profiles the bot and the chats
  that bot is in.
- **Collects:** **admin list ✅** (`getChatAdministrators` with full granular `can_*`
  permissions, `custom_title`, `is_anonymous`), bot intelligence ✅ (`getMe`,
  `can_read_all_group_messages`, `getMyDefaultAdministratorRights`), chat policy flags ✅
  (incl. **`has_hidden_members`**, `has_protected_content`, `join_to_send`,
  `linked_chat_id`), invite links ✅, messages ✅ + media ✅ via Pyrofork, member **count**
  only. Reactions ❌, forwards ❌, edits ❌, stats ❌, similar/boosts/stories ❌.
- **Storage:** stdout / JSON report / **JSONL manifest** + media dir.
- **Caution:** performs **mutating actions on the target** — calls `createChatInviteLink`,
  and derives the latest message id by *sending a "." message and deleting it*. Not passive
  OSINT.

#### telegram-tracker — `estebanpdl/telegram-tracker`
<https://github.com/estebanpdl/telegram-tracker> — **383★**, pushed 2026-04-20 (README only;
last *code* change 2024-08-08).

- **License: none present.** The README badge claims Apache-2.0 but no license file exists in
  the tree, and issue #17 requesting one is unanswered → **effectively all-rights-reserved.
  Do not vendor.**
- **Library:** Telethon; `GetFullChannelRequest` + paged `GetHistoryRequest`, dumping the
  whole Telethon object graph to JSON, then a separate `build-datasets.py` flattens it.
- **Collects:** messages ✅, **forward graph ✅ (a genuine strength** — `channels-to-network.py`
  builds a directed forward network with Louvain community detection), **polls ✅**,
  geolocation ✅ (lat/lng + venue), URLs/link-previews ✅, contacts ✅, media **metadata only
  (no download)**. Reactions land in the raw JSON but are not extracted.
- **Dead code worth noting:** `get_participants_request`, `full_user_req`, `photos_request`
  (GetUserPhotos), `broadcast_stats_req` (GetBroadcastStats), and `get_discussion_message`
  are all *defined and never called* — the author reached for exactly the capabilities we
  identified as the frontier (§1) and never wired them up.
- **Incremental: yes — the only one in cluster A.** `--min_id` + `offset_id` paging lets a
  re-run fetch only new posts, though you persist the cursor yourself.
- **Rate limiting:** one `time.sleep(2)` between channels; no FloodWait handling.
- **Storage:** JSON → CSV/XLSX, ~35 chat columns and ~50 message columns.

#### TeleTracker — `tsale/TeleTracker`
<https://github.com/tsale/TeleTracker> — **541★**, **no license**, last commit 2024-04-29,
**unmaintained ~2 years**.

- **Library:** Bot API (`requests`) + **Pyrogram 2.0.106** (i.e. pinned to the dead upstream,
  layer 158 — see §4.3).
- **Collects:** messages ✅, media ✅, admin list ✅, bot rights ✅, member count only.
  Everything else ❌. Storage is flat TXT + `str(message)` raw dumps — not a queryable schema.
- **Ethics — the most aggressive tool surveyed.** Beyond OSINT it ships offensive
  capabilities: a menu option that spawns up to **1,000,000,000** processes to **spam** a
  channel, mass message deletion, a continuous-send `--spam` loop, and file upload to the
  target. This is exactly the behaviour that gets the whole tool category policed (§4.1).

#### telegram-phone-number-checker — `bellingcat/telegram-phone-number-checker`
<https://github.com/bellingcat/telegram-phone-number-checker> — **1,769★** (most-starred in
cluster A), MIT, 0 open issues, PyPI 1.2.2 (2026-08-17), **actively maintained**.

- **Mechanism:** builds an `InputPhoneContact`, calls `contacts.ImportContactsRequest`, reads
  back matched users, then `contacts.DeleteContactsRequest`. This is a **phone number →
  Telegram account** resolver. v1.2.2 added username lookup and proxy support.
- **Returns:** id, username, **all `usernames[]`**, names, `fake`/`verified`/`premium`/
  `restricted` + `restriction_reason`, humanised last-seen, phone; optional current profile
  photo (not history).
- **Out of scope for channel work** — collects no messages, media, members, or metadata.
- **⚠ Ethics — highest sensitivity in the survey.** This is phone→identity
  de-anonymisation with no subject-consent model and clear stalking/GDPR exposure. The
  README itself concedes the technique trips Telegram anti-abuse: *do not use your personal
  account; a fresh account from residential IPs works best*. **Recommendation: our tool
  should not implement phone→account resolution at all.** It is a different product with a
  different risk profile, it is the single fastest way to get an account banned, and bundling
  it would taint an otherwise defensible channel-archiving tool.

#### Other Bellingcat Telegram tooling (org sweep)
- **`bellingcat/auto-archiver`** — 1,108★, actively maintained. Ships a no-login
  `telegram_extractor` (`t.me/…?embed=1` HTML) *and* a `telethon_extractor` that **handles
  `FloodWaitError` correctly** (`time.sleep(e.seconds)`). Per the subagent this is the only
  repo in cluster A with explicit, correct flood handling — **use it as our reference
  implementation**.
- **`bellingcat/snscrape`** (fork, 350★, GPL-3.0, stale 2024-03-14) — `modules/telegram.py`
  scrapes `t.me/s/<channel>` with no login, yielding posts (url, date, content, outlinks,
  mentions, hashtags, forwarded channel + url, media, views) and a channel entity (title,
  verified, description, members, photo/video/link/file counts). Best prior art for our §3.7
  unauthenticated path.
- **`bellingcat/telegram-group-joiner`** — 61★, MIT, active, **TDLib via `@dibgram/tdweb`
  WASM** in the browser; joins public/private groups. A membership utility, not a scraper.
- **`bellingcat/cisticola`** — 20★, no license, stale (2023-08-08), but architecturally
  relevant: clean **scraper → DB → transformer** separation with a Telethon scraper.

### 2.B The tools that reach the modern signal set (2024–2026)

These matter more than the famous ones, because they are the only prior art touching §1's
frontier.

#### `vognik/maltego-telegram` — the richest modern signal set found anywhere
<https://github.com/vognik/maltego-telegram> — **550★**, GPL-3.0, created 2024-11-04, last
commit 2026-01-27. **Built on Kurigram**, pinned to an exact commit hash.

Delivered as 18 Maltego transforms, not a CLI. Uniquely collects:
- **Similar channels** via `get_similar_channels()` (Kurigram's wrapper over
  `channels.getChannelRecommendations`)
- **Forwards graph** — iterates history collecting `message.forward_from_chat`
- **Admin list**, channel post authors, linked discussion group
- **Deleted-post detection**, cross-referenced to `tgstat.ru/channel/{username}/{post_id}`
  as an external archive
- **Sticker-pack owner de-anonymisation**: `owner_id = sticker_set.set.id >> 32` —
  independently reimplemented by `tgspyder`, so the technique is real
- Media, plus OS inference from image compression artifacts

Missing: reactions, comments, boosts, stories, stats, profile photo history.

#### `chigwell/telegram-mcp` — the broadest raw TL surface
<https://github.com/chigwell/telegram-mcp> — **1,478★**, **Apache-2.0** (the most
permissively licensed feature-rich tool here), created 2025-03-20, last commit 2026-08-19,
actively maintained. Telethon.

An MCP server rather than an OSINT CLI, but it makes **66 distinct `functions.*Request`
calls** — the widest verified TL surface in the survey, including several nobody else
touches: `messages.GetMessageReactionsListRequest` (**reactor identities**),
`channels.GetAdminLogRequest`, `messages.GetMessageReadParticipantsRequest`,
`messages.GetCommonChatsRequest`, `photos.GetUserPhotosRequest`. Treat it as a **reference
catalogue of Telethon call shapes** — and note the licence makes it safe to borrow from.

#### `iyear/tdl` — the largest non-Python tool, and the best single architectural idea
<https://github.com/iyear/tdl> — **7,944★**, **AGPL-3.0**, Go on **gotd/td**, default-branch
last commit 2026-05-23 (release v0.20.3), 790 forks.

Its `--raw` flag is the most important pattern in this corpus:

```go
type Message struct {
    ID   int         `json:"id"`
    Type string      `json:"type"`
    File string      `json:"file"`
    Date int         `json:"date,omitempty"`
    Text string      `json:"text,omitempty"`
    Raw  *tg.Message `json:"raw,omitempty"`   // full TL object when --raw
}
```

The default projection is minimal; `--raw` serialises the **entire `tg.Message`**, which
carries reactions, views, forwards, `fwd_from` and replies for free. Also supports
`--thread` (`Messages().GetReplies(...)`) and `FullRaw(ctx)` for channel metadata. This
independently confirms the §3.3 recommendation to persist raw TL. **AGPL-3.0: read for
technique, do not link.**

#### `SocialLinks-IO/telegram-similar-channels`
<https://github.com/SocialLinks-IO/telegram-similar-channels> — 197★, **no license file →
all-rights-reserved**, last commit 2024-04-10 (stale). Telethon; the explicit
`GetChannelRecommendationsRequest` caller. Ships both a CLI and Maltego transforms, and
enriches each recommendation by scraping `t.me/{username}`.

#### `parvvaresh/telegram-scraper` — best reaction/comment data model
<https://github.com/parvvaresh/telegram-scraper> — only **3★**, GPL-3.0, 2026, Telethon →
**ClickHouse**. Tiny, but the most *correct* schema found: reactions stored **one row per
emoji** (`{reaction: emoticon, count: n}`) rather than fixed columns, and comments via
`iter_messages(channel, reply_to=post_id)` with `is_reply`/`reply_to`/`user_id`. (Its
Iran-specific Shamsi-date/holiday columns should not be copied.)

#### `ergoncugler/web-scraping-telegram`
<https://github.com/ergoncugler/web-scraping-telegram> — 161★, **no license**, Jupyter,
last commit 2024-12-30. The only tool collecting reactions at **both levels** — on posts
*and* on comments.

#### Newer entrants, briefly
- **`Darksight-Analytics/tgspyder`** — 346★, MIT, created 2025-12-14. ⚠ **The entire repo
  is 3 commits, all on its creation date, with nothing in the 8 months since**, despite the
  star count. Reads as a marketing drop. Members, messages, invite-link extraction, sticker
  owner inference, SOCKS proxy. No modern signals.
- **`hamodywe/telegram-scraper-TeleGraphite`** — 275★, MIT, actively maintained
  (2026-08-11). Channel posts → JSON. Narrow but current.
- **`Steelio/Telegram-Post-Scraper`** — 108★, BSD-2-Clause, 2024-11-12. **HTML scraping of
  `t.me/s/`, no API account needed** — corroborates §3.7 as a real fallback path.
- **`robertaitch/telegram-story-scraper`** — 30★, MIT. The only dedicated stories tool, but
  it polls *your own contacts'* stories, not channel stories, and doesn't use
  `stories.getPeerStories`. **Channel stories remain uncovered by any open-source tool.**
- **`sockysec/Telerecon`** — **1,322★** but **no license** and stale since 2024-04-22. Its
  "network analysis" is a *user-to-user* interaction graph read from a pre-existing CSV, not
  a channel forward graph. Given stars vs. staleness, the most over-cited tool in the space.
- **`Alb-310/Geogramint`** — 729★, GPL-3.0, **archived** 2024-09-07 (geo/nearby; consistent
  with Telegram removing People Nearby).
- **`bret99/telegram_scan`** — 75★, MIT, 2026-05-15, but built on **archived Pyrogram** →
  layer 158 (§4.3).

### 2.C Explicitly checked and negative

- **`vvmnnnkv/telegram-channels-aggregator` does not exist** — `GET /repos/…` → 404, no
  redirect. Remove from any list.
- **`unnohwn/telegram-scraper` is gone** — repo *and* user account 404 (independently
  verified twice). The most-cited Telethon+SQLite scraper in 2025 blog posts and still
  linked from live awesome-lists. Surviving mirror: `ThBroth/telegram-scraper` (4★ but
  **210 forks** — the inversion is the tell). ⚠ Blog posts claim it captured reactions,
  views, forwards and post authors; the surviving schema has **11 columns and no
  reactions** (`grep -i reaction` returns nothing). **Treat the reactions claim as
  UNVERIFIED/likely false.**
- **DISARM Foundation ships no Telegram collection tooling** — all 14 org repos enumerated;
  taxonomy/STIX only. Useful downstream as a TTP vocabulary for *tagging* findings
  (`DISARMFoundation/DISARMframeworks`, 279★, CC-BY-SA-4.0), never as a collector.
- **No open-source tool calls `premium.getBoostsList`/`getBoostsStatus`** — code search
  returned only vendored library copies.
- **No open-source tool calls `stories.getPeerStories` for channel stories.**
- **No open-source tool calls `stats.getMessagePublicForwards`.**
- `unnohwn/telescraper` and `DarkWebInformer/telegram-scraper` — both 404.

### 2.D Awesome-lists are a trap

| List | Stars | Last commit | Verdict |
|---|---|---|---|
| `ItIsMeCall911/Awesome-Telegram-OSINT` | **2,835** | **2021-12-13** | Content ~4.7 years old. The most-linked list in the space and by far the most stale. Its many forks are copies — treat as one source, not five. |
| `The-Osint-Toolbox/Telegram-OSINT` | 1,950 | **2026-05-17** | The only genuinely current list; best starting point. Even so, 2 of its 18 entries are dead (`unnohwn/telegram-scraper` 404, `Geogramint` archived). |
| `cipher387/osint_stuff_tool_collection` | 8,702 | 2026-05-12 | Active but general-OSINT; thin on Telegram. |

---

## 3. Architecture lessons

### 3.1 `min` users and `access_hash` — the single biggest correctness trap

Per <https://core.telegram.org/api/min>: `user#` and `channel#` constructors carry a
`min` flag (`user#...min:flags.20?true`) when returned in a reduced form for
performance/privacy reasons — typically exactly the case we care about, "users seen as
message senders inside a channel." A `min` object's `access_hash` **is not usable** to
build a normal `inputPeerUser` for follow-up calls like `users.getFullUser` or
`photos.getUserPhotos`.

The documented workaround is to store the *context* in which the peer was seen and later
construct `inputPeerUserFromMessage` / `inputUserFromMessage` /
`inputPeerChannelFromMessage` / `inputChannelFromMessage`, passing `peer` = the channel
and `msg_id` = the message in which the user was observed.

**Design implication:** our peer table must not be `(user_id, access_hash)`. It must be
`(user_id, access_hash, is_min, first_seen_channel_id, first_seen_msg_id)`, and the
enrichment pass must fall back to `*FromMessage` constructors for `min` peers. Tools
that store only `access_hash` silently fail to enrich the majority of channel-observed
users. This also means **peer identity is per-account**: an `access_hash` harvested by
session A is meaningless to session B, so the session identity must be a column, not an
assumption.

### 3.2 Incremental sync: `pts` is the right primitive, `last_message_id` is not

Almost every tool in the survey resumes by storing the highest message id seen and
re-running `getHistory` forward from it. That captures *new* messages and nothing else —
it can never observe an edit or a deletion, which for OSINT are often the most
interesting events (a channel quietly editing or deleting a post is signal).

The correct primitive is the channel's `pts`, available from `channels.getFullChannel`
and carried by every update. Store it, and on each run call
`updates.getChannelDifference(channel, pts, limit)` to receive
`updateEditChannelMessage`, `updateDeleteChannelMessages`, `updateChannelMessageViews`
and `updateChannelMessageForwards` in addition to new messages.

Two caveats, both documented:
- Handle `updates.channelDifferenceTooLong` by re-reading state and backfilling (§1.4).
- The docs say *"do not re-invoke `updates.getChannelDifference` if the returned
  difference is final, unless the user has opened the channel"* — a polling archiver
  intentionally violates the client contract here, so poll on a sane interval.

A belt-and-braces complement: periodically re-probe id ranges with
`channels.getMessages` and record any id that comes back as `messageEmpty` as deleted.
This catches deletions that fell outside the `pts` window.

### 3.3 Storage schema

The consensus best-in-class choice among the surveyed tools is **SQLite**, and it is the
right one. Concretely, the schema should be normalised around these facts:

- **Messages are versioned, not overwritten.** A single `messages` table keyed on
  `(channel_id, msg_id)` loses edit history. Use `messages` (current state) +
  `message_revisions` (append-only, keyed `(channel_id, msg_id, seen_at)`) so an edit is
  a new row and a deletion is a tombstone with `deleted_at`. This is the schema
  difference that separates an archiver from an OSINT tool.
- **Peers are global and per-session.** `peers(peer_id, kind, access_hash, is_min,
  session_id, seen_in_channel, seen_in_msg)` — see §3.1.
- **Counters are time series.** `views`, `forwards`, `reactions`, `participants_count`,
  `boosts_applied` all change over time. Storing the latest value throws away the most
  analytically useful data. `message_metrics(channel_id, msg_id, observed_at, views,
  forwards)` and `channel_metrics(channel_id, observed_at, subscribers, online_count)`
  cost almost nothing and are irrecoverable if not captured.
- **Raw TL should be preserved.** Store the serialised TL object (or a JSON projection)
  alongside the parsed columns. Layer 228 has 21 `MessageMedia` variants and 52
  `ChannelAdminLogEventAction` variants and will grow; a tool that only persists the
  fields its current parser understands loses data permanently on every new media type.
  This is the mistake that makes stale tools (§4.3) lossy rather than merely broken.
- **Store `sqlite` in WAL mode** so a long-running sync doesn't block a concurrent
  read/query CLI.

Emitting JSONL/CSV/HTML should be a *view* over SQLite (an `export` subcommand), never
the primary store. Tools that write JSON/CSV directly (§2) cannot do incremental
updates, dedup, or edit tracking without re-reading and rewriting whole files.

### 3.4 Media: dedup and `file_reference` expiry

- **Dedup key.** `photo#` and `document#` both expose a stable `id:long`. Within one
  account's view that is a reliable dedup key, and it is free — no download needed.
  Combine with a content hash (sha256) computed on write for cross-account dedup, and
  store media content-addressed (`media/<sha256[0:2]>/<sha256>`) with a join table
  mapping `(channel_id, msg_id) → media_id`. Forwarded/reposted identical files then
  cost one copy. `grouped_id` reassembles albums.
- **`file_reference` expires.** `photo#`/`document#` carry `file_reference:bytes`, and
  downloads fail with `400 FILE_REFERENCE_EXPIRED` ("must be refetched as described in
  the documentation"). **Never persist a `file_reference` and expect to use it later** —
  on failure, re-fetch the containing message with `channels.getMessages` to obtain a
  fresh reference and retry. A media backlog processed hours after the message scan will
  hit this constantly.
- **`FILE_MIGRATE_%d` / `dc_id`.** Media lives on a specific DC (`document.dc_id`), and
  downloads may return `400 FILE_MIGRATE_X`. A downloader needs per-DC sender pools.
  Notably, grammers shipped a fix for exactly this on 2026-07-13 ("Use DC from
  Downloadable to mitigate FILE_MIGRATE_ERROR").
- **`stats_dc`.** Separately, `channelFull.stats_dc` means `stats.getBroadcastStats`
  must be routed to a *different DC* than the home DC. This trips up naive
  implementations.
- **Free thumbnails.** `stripped_thumb:bytes` on `chatPhoto`/`userProfilePhoto` and
  `PhotoSize` types give an inline preview with **zero** download calls — worth storing
  always, even when full media download is disabled.

### 3.5 FLOOD_WAIT and rate limiting

- The error is `FLOOD_WAIT_%d` — *"Please wait %d seconds before repeating the action."*
  There is also `FLOOD_PREMIUM_WAIT_%d` (removable by buying Premium) and `420
  FROZEN_METHOD_INVALID` for frozen accounts.
- **`PEER_FLOOD` is the one that matters.** Its official description: *"The current
  account is spamreported, you cannot execute this action, check @spambot for more
  info."* This is an account-level restriction, not a backoff signal. If a run produces
  `PEER_FLOOD`, stop; do not retry.
- Telethon's design is worth copying: `flood_sleep_threshold` (default **60s**) — sleep
  through short waits automatically, raise on long ones so the caller can decide. Its
  `_flood_waited_requests` dict is keyed by **`CONSTRUCTOR_ID`**, i.e. flood waits are
  tracked *per method*, and a pre-flight check skips a request that is known to still be
  in a flood window (waits ≤3s are ignored, ≤threshold are slept, longer raise).
  Copy this: a global sleep is wrong, because being flooded on `getParticipants` says
  nothing about `getHistory`.
- Telethon also wraps requests in `InvokeWithoutUpdatesRequest` when updates aren't
  needed — cheap server-side load reduction that a bulk scraper should adopt for its
  history-scan phase (but *not* for the `getChannelDifference` phase).
- Practical policy for us: single sequential worker per channel, exponential backoff on
  top of the server's stated wait, a persisted per-method cooldown table so a restart
  doesn't immediately re-trip the same wall, and a hard daily budget.

### 3.6 Session management and proxies

- Telethon's config surface is a good checklist: `session` (SQLite or `StringSession`),
  `proxy` (via `python-socks`, SOCKS5/SOCKS4/HTTP/MTProxy), `use_ipv6`, `local_addr`,
  `connection_retries=5`, `request_retries=5`, `auto_reconnect=True`,
  `device_model`/`system_version`/`app_version` (these are attacker-visible fingerprints
  and are sent in `initConnection`), `receive_updates`, `entity_cache_limit=5000`.
- **Store the session outside the data directory** and treat it as a credential.
  A `.session` file is a full account authorisation; several surveyed tools cheerfully
  write it next to the output and users commit it.
- Proxy support is non-optional for operators in restricted jurisdictions — Telethon's
  own FAQ notes connection failures reported from Kazakhstan and China.
- `device_model`/`app_version` defaults that scream "Telethon" are a fingerprint; set
  them deliberately.

### 3.7 The unauthenticated path: `t.me/s/<channel>`

Verified live on 2026-08-20:

- `GET https://t.me/s/durov` → HTTP 200, 142 KB, **20 posts per page**, each with
  `data-post="durov/523"`, `tgme_widget_message_date` (exact ISO timestamp in the
  `datetime` attribute), `tgme_widget_message_views` (e.g. `12.5M`),
  `tgme_widget_message_from_author`, media thumbnails, and link previews. Page header
  carries `tgme_header_counter` → `11.1M subscribers`.
- Pagination confirmed working: `?before=<msg_id>` (returned ids 373…399) and
  `?after=<msg_id>` (returned 401, 402…).
- `GET https://t.me/<channel>` (no `/s/`) → OG tags: `og:title`, `og:description`
  (channel bio), `og:image` (avatar CDN URL), and `tgme_page_extra` → exact
  `11 143 133 subscribers`.
- `GET https://t.me/<channel>/<id>?embed=1&mode=tme` → single-post render.

**Design implication:** this is a zero-auth, zero-ban-risk baseline that works for any
public channel, and it independently corroborates MTProto results. A serious tool should
run it as a *first pass* (cheap discovery + existence + subscriber count + rough
timeline) and only escalate to an authenticated MTProto session for depth. Views are
rounded (`12.5M`) so MTProto remains authoritative for exact counters. Note that only
public channels are exposed, and a channel can be excluded from the web preview.

---

## 4. Pitfalls

### 4.1 Account bans and limitations

Telethon's own FAQ (`readthedocs/quick-references/faq.rst` on Codeberg) is the most
honest primary source and should be quoted in our README:

> "**this is not a problem exclusive to Telethon. Any third-party library is prone to
> cause the accounts to appear banned.** Even official applications can make Telegram ban
> an account under certain circumstances."

> "More recently (year 2023 onwards), Telegram has started putting a lot more measures to
> prevent spam (with even additions such as anonymous participants in groups or the
> inability to fetch group members at all). This means some of the anti-spam measures
> have gotten more aggressive."

> "The recommendation has usually been to use the library only on well-established
> accounts (and not an account you just created), and to not perform actions that could
> be seen as abuse."

It also flags elevated ban risk for numbers from Iran and Russia, for VoIP/virtual
numbers, and recommends `@SpamBot` as the way to check whether an account is limited.
`PeerFloodError` is named as the observable symptom.

Practical mitigations for our design: prefer the unauthenticated web path where it
suffices; never bundle any "add members" capability (that is what actually gets accounts
killed, and it is the reason `getParticipants` is policed so hard); default to
conservative pacing; make the tool refuse to run a member-enumeration pass on a session
younger than N days; surface `PEER_FLOOD` as a hard stop with a link to `@SpamBot`.

### 4.2 "Hide members" and the broadcast-channel wall

Two distinct things get conflated in the wild:

1. **Broadcast channels never expose subscribers to non-admins.** `errors.json` lists
   `400 CHAT_ADMIN_REQUIRED` and `403 CHAT_ADMIN_REQUIRED` for `channels.getParticipants`.
   This is unconditional. Tools and vendors that advertise "scrape channel members" are
   either operating on supergroups, or reconstructing from message senders, or lying.
2. **Supergroups can hide their member list** (`channelFull.participants_hidden`,
   toggled by `channels.toggleParticipantsHidden`, available above
   `hidden_members_group_size_min` participants). This broke a generation of scrapers
   that assumed a non-zero `participants_count` implied an enumerable list.

The honest fallbacks, all of which we should implement and label as *partial*:
- Unique `from_id` over the message history (works whenever you can read messages).
- `recent_repliers` on `messageReplies`, and the senders in the linked discussion
  supergroup via `messages.getReplies` — for broadcast channels this is often the only
  route to *any* human identities.
- `messageService` actions in supergroups: `messageActionChatAddUser`,
  `messageActionChatJoinedByLink`, `messageActionChatDeleteUser` give **join/leave
  events with timestamps and inviters** that survive `participants_hidden`.
- `recent_reactions` (a short vector of `MessagePeerReaction` with `peer_id` and `date`)
  — partial, but free, and the full list is forbidden in broadcasts anyway.

Any tool must report coverage honestly: "412 of ~48,000 subscribers identified, via
message senders and comment threads" is a legitimate result; "48,000 members scraped" is
not.

### 4.3 TL layer lag = silent data loss

Layer currency, verified 2026-08-20 by reading each project's own `api.tl` / schema:

| Library | Layer | Evidence |
|---|---|---|
| Telethon (Codeberg `v1`) | **228** | `telethon_generator/data/api.tl`; commit "Update to layer 228" 2026-07-11 |
| Kurigram | **228** | `compiler/api/source/main_api.tl` |
| TDLib master | **228** | `td/telegram/Version.h`: `constexpr int32 MTPROTO_LAYER = 228;` |
| gotd/td (Go) | **228** | `_schema/telegram.tl` |
| grammers (Codeberg) | **228** | commit "Update to layer 228" 2026-07-11 |
| teleproto (TS) | **228** | `teleproto_generator/static/tl/api.tl` |
| pyrofork | 220 | `compiler/api/source/main_api.tl` |
| GramJS (archived) | 193 | last layer bump 2024-11-19 |
| Pyrogram (archived) | **158** | `compiler/api/source/main_api.tl` |

This is not cosmetic. Layer 228 defines 21 `MessageMedia` constructors including
`messageMediaPaidMedia`, `messageMediaToDo`, `messageMediaVideoStream`,
`messageMediaGiveaway`/`GiveawayResults`, and `messageMediaStory`. A library at layer 158
cannot deserialise them — the message arrives as `messageMediaUnsupported` or fails
outright, and the content is **lost, not deferred**. Likewise `channels.searchPosts`,
`channels.getChannelRecommendations` (similar channels), `premium.getBoostsList`,
`messages.getFactCheck`, and the entire `stories.*` namespace simply do not exist below
their introduction layers.

Corollary for our design: pin a layer-228+ library, and persist raw TL (§3.3) so a future
layer bump can re-parse historical captures rather than re-scrape them.

### 4.4 Ecosystem churn (the 2026 reshuffle)

| Project | GitHub status | Where it actually lives now |
|---|---|---|
| `LonamiWebs/Telethon` | **Archived**, 12,064★, MIT, last commit 2026-02-21 = *"Migrate off GitHub"* | <https://codeberg.org/Lonami/Telethon> (123★ there, active 2026-08-19) |
| `Lonami/grammers` (Rust) | **Archived**, 816★, Apache-2.0, last commit 2026-02-10 = *"Migrate off GitHub"* | <https://codeberg.org/Lonami/grammers> (61★, active 2026-08-09, v0.10.0) |
| `pyrogram/pyrogram` | **Archived**, 4,620★, LGPL-3.0, last push 2024-12-23, 279 open issues | Fork: `KurimuzonAkuma/pyrogram` ("Kurigram") |
| `gram-js/gramjs` | **Archived**, 1,768★, MIT, last push 2026-07-14, 323 open issues | Fork: `sanyok12345/teleproto`, 309★, MIT, active |

Two of these archives are **misleading**: Telethon and grammers were not abandoned, they
were moved off GitHub by their author. Star counts and "last commit" on GitHub are now
actively wrong signals for these two projects. A dependency-health check that only looks
at GitHub will reach the wrong conclusion.

**Supply-chain note:** the low Codeberg star counts (123 and 61) reflect a fresh forge,
not a dead project — but they do mean the drive-by review pressure on those repos is
lower than it was. Pin versions and hashes.

### 4.5 Tools that simply vanish

`unnohwn/telegram-scraper`, one of the most-cited recent Telethon+SQLite scrapers,
returns **HTTP 404 for both the repo and the user account** as of 2026-08-20 (verified
via `gh api users/unnohwn` → 404 and a direct HTTPS fetch → 404). Its design is
recoverable only from forks and mirrors. This is a recurring pattern in this space — the
`th3unkn0n/TeleGram-Scraper` repo is archived, `expectocode/telegram-export` now
redirects to `tnjd/telegram-export` (archived, last push 2019-10-27). Do not build on a
dependency from this ecosystem without vendoring it.

---

## 5. Recommended library for 2026

**Primary recommendation: Telethon 1.44.x from PyPI, tracking the Codeberg `v1` branch.**

Evidence:
- Layer **228** — fully current, matches TDLib master.
- Shipping: PyPI `Telethon` 1.44.0 on **2026-06-15**, preceded by 1.43.x (2026-04),
  1.42.0 (2025-11-05), 1.41.x (2025-09) — a steady cadence, 240 releases total.
- Active development: Codeberg `v1` last commit **2026-08-19**, only 5 open issues.
- MIT licensed; pure Python (no build toolchain); `requires_python >= 3.5`.
- Best-in-class raw-API access (`telethon.tl.functions.*` covers the whole schema, so
  `stats.getBroadcastStats`, `premium.getBoostsList`, `channels.getChannelRecommendations`
  are all reachable even without high-level wrappers), plus a mature
  `flood_sleep_threshold` model (§3.5) and `*FromMessage` support for `min` peers (§3.1).
- Largest body of prior art to borrow from — nearly every tool in §2 is Telethon-based.

**Do not build on Telethon v2 yet.** The `v2` branch declares version **`2.0.0-a0`**
(alpha) in `Cargo.toml`, requires **Python ≥3.12**, is dual MIT/Apache-2.0, and — the
important part — as of 2026-06-18 its **sender was replaced with grammers**, making v2 a
`pyo3` Rust extension rather than pure Python. Commits from 2026-08-17 are still "Fix
various issues encountered when building wheels." **There is no 2.x release on PyPI at
all** (verified: zero of 240 published versions start with `2`). It is the right future
target and worth an abstraction seam, but not a foundation today.

**Strong second: Kurigram (`KurimuzonAkuma/pyrogram`, PyPI `Kurigram` 2.2.24,
2026-07-11).** Layer 228, 798★, LGPL-3.0, commits landing daily (last: 2026-08-20). Its
*high-level* API is meaningfully richer than Telethon's for our use case — it ships
`get_similar_channels`, `get_chat_event_log`, `get_personal_channels`,
`get_chat_online_count`, `get_chat_photos` as first-class methods, where Telethon would
need raw TL. The trade-offs are LGPL-3.0 (vs MIT) and a much smaller maintainer pool
(effectively one very active maintainer). Reasonable choice if you value ergonomics over
licence permissiveness.

**Rejected, with reasons:**

| Option | Verdict |
|---|---|
| Pyrogram (upstream) | **Dead.** Archived, layer 158, last push 2024-12-23. Disqualifying. |
| pyrofork | Layer 220, commits stopped 2025-12-10 despite a 2026-07-01 push. Behind and slowing. |
| GramJS | **Archived** with a deprecation notice; layer 193. |
| teleproto (TS) | Genuinely current (layer 228, MIT, active) — the right answer *if* the tool were Node/TS. 309★ and one maintainer; young. |
| gotd/td (Go) | Excellent engineering, layer 228, MIT, 2,313★, pushed 2026-08-20. Best choice for a single-binary distributable CLI. Costs us the Python prior-art corpus and a much smaller OSINT-example base. Worth a second look if distribution simplicity outweighs velocity. |
| grammers (Rust) | Layer 228, active on Codeberg, v0.10.0, Apache-2.0. Now serves as Telethon v2's engine. Low-level; you'd be rebuilding a lot. |
| TDLib | Layer 228, v1.8.66, BSL-1.0, 9,039★, active. The most *correct* implementation and the only one with a real local database and update-gap handling built in. But it is a C++ dependency with a heavyweight JSON-in/JSON-out interface, its object model is TDLib's abstraction rather than raw TL (so some raw fields we want are simply not surfaced), and calling raw MTProto methods through it is awkward. Overkill here. |
| hydrogram | Another Pyrogram fork. Layer **223**, LGPL-3.0, 244★, last commit 2026-03-22 — behind Kurigram on both currency and velocity. No reason to prefer it. |
| pyrofork | (see above) Layer 220, stalled. |
| WTelegramClient (C#) | MIT, 1,313★, pushed 2026-08-18 — genuinely active. TL layer **UNVERIFIED** (could not extract a layer constant cleanly). Wrong runtime for us. |
| MadelineProto (PHP) | 3,493★, **AGPL-3.0**, active (2026-08-17). Licence alone rules it out for most distributions. |
| Bot API (`python-telegram-bot` 22.8) | Wrong tool entirely. A bot cannot read a channel's history it wasn't present for, cannot enumerate participants, cannot see most metadata. Non-starter for this brief. |

**Recommended posture:** build against Telethon 1.44.x, but put every API call behind a
thin internal client interface. Persist raw TL. That way the eventual migration to
Telethon v2 (or a swap to Kurigram/gotd) is a single-module change, and — per §3.3 — a
layer bump lets us re-parse the archive instead of re-scraping it.

---
