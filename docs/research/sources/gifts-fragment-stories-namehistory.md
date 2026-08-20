# Star gifts, Fragment/TON, stories, name history — MTProto user-account surface (condensed)

Schema baseline layer 223, verified against https://core.telegram.org/schema.
Access: **Public** = any account that can resolve the peer; **Self** = own account;
**Admin** = channel admin.

## A. Star gifts

| Data | Method / field | Access | Caveat | Source |
|---|---|---|---|---|
| Gifts a user *displays* | `payments.getSavedStarGifts(peer, offset, limit)` | Public | "including users different from us"; only *displayed* gifts for third parties | core.telegram.org/method/payments.getSavedStarGifts |
| Hidden (`unsaved`) gifts | same, `exclude_saved/exclude_unsaved` | Self / owned peers | | core.telegram.org/api/gifts |
| Reception date | `savedStarGift.date` | Public | Timeline anchor | core.telegram.org/constructor/savedStarGift |
| Sender | `savedStarGift.from_id` | Public unless `name_hidden` | Recipient still sees sender | same |
| Message, pinned, collections | `.message`, `pinned_to_top`, `collection_id` | Public unless hidden | | same |
| `userFull.stargifts_count` | | Public | Displayed count, not received | core.telegram.org/constructor/userFull |
| Gift privacy posture | `userFull.disallowed_gifts`, `display_gifts_button` | Public | | core.telegram.org/api/gifts |

### Collectible (unique) gifts — the Telegram → TON bridge

| Data | Method / field | Access | Source |
|---|---|---|---|
| Resolve by slug, no ownership check | `payments.getUniqueStarGift(slug)` → `gift, chats, users` (owner resolved to full `User`) | Public | core.telegram.org/method/payments.getUniqueStarGift |
| Owner peer | `starGiftUnique.owner_id` | Public | core.telegram.org/constructor/starGiftUnique |
| **Owner TON wallet** | `starGiftUnique.owner_address` | Public | same |
| **NFT item address** | `starGiftUnique.gift_address` | Public | same |
| Original sender/recipient/date — survives transfers | `starGiftAttributeOriginalDetails{sender_id, recipient_id, date, message}` | Public (scrubbable for a fee) | core.telegram.org/constructor/starGiftAttributeOriginalDetails |
| Rarity / attributes | `starGiftAttributeModel/Pattern/Backdrop/Rarity permille` | Public | core.telegram.org/api/gifts |
| Sale / price history | `payments.getUniqueStarGiftValueInfo(slug)` → initial/last sale date+price, `fragment_listed_url` | Public | core.telegram.org/api/gifts |
| Hosted ≠ owned | `host_id`; filter with `exclude_hosted` | Public | same |
| Bulk owner harvest by gift type | `payments.getResaleStarGifts(gift_id, attributes…)` | Public | core.telegram.org/method/payments.getResaleStarGifts |
| `t.me/nft/<slug>` | Public web page, no login: owner name+avatar, model/backdrop/symbol rarity, issued count; **no TON address** (MTProto-only) | None | core.telegram.org/api/links |

`payments.getUserStarGifts` is **removed** (≤ layer 198) — use `getSavedStarGifts`.

## B. Fragment / TON

| Data | Method | Access | Caveat | Source |
|---|---|---|---|---|
| Is a username a Fragment collectible | `username.editable` **absent** ⇒ bought on Fragment (`user.usernames` vector only) | Public | Single-collectible holders populating `usernames`: UNVERIFIED | core.telegram.org/constructor/username |
| Purchase date + price of a collectible username / +888 number | `fragment.getCollectibleInfo(inputCollectibleUsername|Phone)` → `purchase_date, currency, amount, crypto_currency, crypto_amount, url` | Public per /api/fragment ("or other users"); method page wording conflicts — **UNVERIFIED, smoke-test** | First purchase only; returns Fragment URL, not wallet | core.telegram.org/method/fragment.getCollectibleInfo |
| Collectible emoji status → gift slug | `user.emoji_status` = `emojiStatusCollectible{collectible_id, slug, …}`; live `updateUserEmojiStatus` | Public | Pivot: profile → slug → `getUniqueStarGift` → `owner_address` | core.telegram.org/api/emoji-status |
| Fragment web pages | `fragment.com/username/<n>`, `/number/<n>` | Public | Bid history with bidder TON wallets; current owner wallet on sold page UNVERIFIED | fragment.com/about |

## C. Stories

| Data | Method | Access | Caveat | Source |
|---|---|---|---|---|
| Active stories of a peer | `stories.getPeerStories(peer)` | Any user | Privacy filtered server-side; non-contact vs contacts-only: UNVERIFIED | core.telegram.org/method/stories.getPeerStories |
| **Pinned profile stories (outlive 24 h)** | `stories.getPinnedStories(peer, offset_id, limit)` | Any user | The durable story surface | core.telegram.org/api/stories |
| Archive | `stories.getStoriesArchive` | Self / `edit_stories` admin | | core.telegram.org/method/stories.getStoriesArchive |
| Viewer list | `stories.getStoryViewsList` | Self only | | core.telegram.org/method/stories.getStoryViewsList |
| Aggregate views on third-party stories | `storyItem.views` | UNVERIFIED | TDLib: may be null if not owned | |
| Reactions list | `stories.getStoryReactionsList` | Channel admin | | |
| **Global story search by hashtag or GEO area** | `stories.searchPosts(hashtag | area:mediaAreaGeoPoint/Venue)` → `foundStory{peer, story}` | Any user, no Premium | Geofence-first discovery of strangers — ethically weigh | core.telegram.org/method/stories.searchPosts |
| Deep link | `t.me/<username>/s/<story_id>` | Public | | core.telegram.org/api/links |
| Presence flags | `user.stories_max_id`, `stories_unavailable`, `userFull.stories_pinned_available` | Public | `stories_hidden` is *our* setting, not theirs | core.telegram.org/constructor/userFull |

`storyItem` fields: `pinned, public, close_friends, contacts, selected_contacts, noforwards, edited, id, date, expire_date, from_id, fwd_from, caption, entities, media, media_areas, privacy, views, albums`.
Media areas: `mediaAreaGeoPoint{geo lat/long + geoPointAddress{country_iso2, state, city, street}}`, `mediaAreaVenue{provider, venue_id}`, `mediaAreaWeather{temperature_c}`, `mediaAreaChannelPost{channel_id, msg_id}`, `mediaAreaStarGift{slug}`, `mediaAreaUrl`.

## D. Username / display-name history — official answer is NO

- Zero history-bearing fields on `user`/`userFull`; all 15 `prev_value` fields in
  the schema belong to `channelAdminLogEventAction*` (admin-only, 48 h retention,
  and none record a *participant's* own name change). None of the 64
  `MessageAction` constructors record name changes either.
- `contacts.resolveUsername` returns the current holder only; released handles are
  re-claimable in ~15–30 min, so an old handle resolving to X is zero evidence.
- `updateUserName` is push-only, new values only, delivered only for peers your
  session has materialised — history starts the day you start watching.
- **Official retrospective identity signals that do exist:** `photos.getUserPhotos`
  (dated profile-photo history, privacy-gated, user can wipe);
  `messageFwdHeader.from_name` / `post_author` (display names frozen at forward
  time inside public messages).
- Third-party bots (@SangMata_beta_bot etc.) record names they have *observed*;
  coverage is unknowable, they log your queries, and at least one open-source
  "clone" fabricates random-dated history. Treat as unsourced.
- Account-age-from-ID services interpolate sparse community anchor sets
  (~212 anchors, 2013→2025); emit an interval, never a bare date.

## Design implications

1. Strongest fully-public chain: `emoji_status` slug → `getUniqueStarGift` → owner
   peer + TON wallet + NFT address + original sender/recipient/date →
   `getUniqueStarGiftValueInfo` → sale dates/prices.
2. Model three visibility tiers explicitly (public / self / admin) and the
   false-positive traps: `host_id` ≠ owner, `stargifts_count` = displayed,
   `stories_hidden` = ours, `personal_photo` = ours.
3. Persist pinned stories (indefinite, public, carry lat/long + venue + weather).
4. Name history: build the honest negative in — store `first_seen`/`last_seen`
   per `(user_id, name)` so "never renamed" ≠ "not observed".
5. Key identity on `user_id`, never on username.
6. Smoke-test before claiming access levels: `getPeerStories` as non-contact;
   `storyItem.views` on others' stories; `getUserPhotos` on a restricted
   non-contact; `fragment.getCollectibleInfo` on a stranger's collectible;
   `updateUserName` delivery scope.
