# `discussion` collector — design spec

Phase 2. Collects a broadcast channel's linked discussion group: the comment
threads and, more importantly, the people in them. Extends the approved design
(`2026-08-20-paperboy-design.md` §6 line 170, §7) rather than replacing any part
of it.

## 1. Purpose

For a broadcast channel, a non-admin account can enumerate **nothing** about
subscribers — not the member list, not even the admin list
(`docs/research/sources/mtproto-participants-users.md` §1.3, sourced to TDLib).
The linked discussion group is therefore *the* person vector, and reading it
never requires joining unless the group sets `join_to_send`
(`docs/research/telegram-extraction-surface.md` line 114).

This collector exists to turn that fact into stored entities: comment messages,
their authors as `peers`, and edges tying a person to the channel post they
engaged with.

## 2. Scope

**In scope.** Full resumable sweep of the linked group; comment→post mapping;
`commented_on` and `replied_to` edges; a zero-RPC `recent_repliers` backfill
from already-captured payloads.

**Out of scope, deliberately.** Member enumeration of the group
(`channels.getParticipants` — that is the `participants` collector); per-user
profile fetches (`profiles`); reactions (`BROADCAST_FORBIDDEN` on channels, and
groups-only, so it belongs with `participants`); joining anything.

## 3. Settled decisions

| Decision | Choice | Why |
|---|---|---|
| Sweep depth | Full resumable bulk `getHistory` on the linked group | Spec §6 line 170. Group-native chatter is membership evidence that per-post thread fetches miss. |
| Relationship to `history` | **Generalize `HistoryCollector`** to take an explicit target | One page loop, one set of resumability semantics, no duplicated FLOOD_WAIT/cursor logic that would drift. |
| Deletion gap-probing | **Off** for the discussion group | On ~35k ids where members delete routinely, probing is a second pass the size of the sweep. `evidence='gap'` is documented as the weak tier anyway (`docs/data-model.md`). |
| Storage | Reuse `messages` under `channel_id = <group id>` | **No migration.** `(channel_id, msg_id)` is already the unique index; `reply_to_msg_id`/`reply_to_top_id` already exist; `sync_state`/`sync_ranges` are already keyed by channel id. |
| Re-collection | **High-water mark**: `max_id_seen` alongside `offset_id` | Without it the sweep is one-shot. `HistoryCollector` never resets its cursor on loop exhaustion, so after a full sweep it pages strictly *older* forever. The channel is rescued by pts-based `catch_up()`; the linked group has no pts seed, so it would freeze at day one. |
| Edge shape | Both `person → post` and `comment → parent` | Two questions, two predicates, each answerable without a join. Both are in the `docs/data-model.md` vocabulary. |

## 4. Preflight

1. Read `linked_chat_id` from `channels` for `ctx.channel_id`. Absent, `0`, or
   `NULL` → `CollectResult(stopped=...)`, a clean skip: not every channel has a
   discussion group.
2. Build `input_channel` for the group from the `peers` row's `access_hash` —
   already captured from ChatFull's `chats` array by the `channel` collector.
   **No `resolve` RPC.** If there is no `peers` row, or `access_hash` is absent
   **or falsy** (a stored `0` is not a usable hash — it yields `CHANNEL_INVALID`
   against live Telegram, a phase error rather than the clean skip promised
   here), skip and record.

   **The two skip reasons must be lexically disjoint**, because tests assert on
   them: the no-group reason is `"no linked discussion group"` and the hash
   reason is `"discussion group {id}: no access hash known"`. Neither is a
   substring-match for the other. A reason containing both "linked" and "access"
   would let a test pass on the wrong branch — including on the falsy-`0` bug
   above.
3. If the group's `join_to_send` flag is set, reading requires membership.
   `SkipAndRecord` — never a hard failure, and never an implicit join.

## 5. Generalizing `HistoryCollector`

```python
async def collect(
    self,
    ctx: CollectContext,
    *,
    channel_id: int | None = None,
    input_channel: dict | None = None,
    probe_gaps: bool = True,
) -> CollectResult
```

`None` defaults fall back to `ctx.channel_id` / `ctx.input_channel`, so current
behaviour is bit-for-bit unchanged.

**The regression contract: every existing test in `tests/test_collector_history.py`
and `tests/test_history_catchup.py` must pass unmodified.** A change to any of
them is a design violation, not a test fix — this is the only part of this
feature with blast radius into shipped Phase 1 code.

`sync_state` scope stays `"history"`, keyed by the *target* channel id, so the
channel and its group resume independently without a new namespace. Its value
gains two fields:

```python
{"offset_id": int, "max_id_seen": int, "backfill_complete": bool}
```

**Two modes, one page loop.** They differ only in the stop condition:

- **Backfill** (`backfill_complete` false) — page oldest-ward from `offset_id`
  exactly as today. Stop on an empty page; on that stop set
  `backfill_complete = True` and reset `offset_id = 0`.
- **Incremental** (`backfill_complete` true) — page from `offset_id = 0`, i.e.
  newest-first, and stop as soon as the page's lowest id is `<= max_id_seen`.
  Reset `offset_id = 0` on that stop so the next run also starts from newest.

`max_id_seen` advances to the highest id observed in either mode and is
persisted with the cursor on every page, so a run killed mid-page resumes
without re-fetching what it already stored.

Absent state, both fields default to `offset_id = 0`, `max_id_seen = 0`,
`backfill_complete = False` — which is exactly today's behaviour, so `history`
is unaffected.

**`add_range` is called only when `probe_gaps` is true.** `store/sync.py`
documents a range as "verified-complete — every id in the span was either
stored or probed". With probing off that invariant is false, so writing the
range would make `missing_ids()` report zero gaps for the group forever. The
discussion group therefore gets no `sync_ranges` rows at all.

**Comment authors that are channels.** `_observe_message` currently upserts a
peer only for a `PeerUser` `from_id`. Anonymous and channel-authored comments
carry `PeerChannel`, and they are people-discovery data too, so the projection
widens to both. This lives here, in `history.py`, because that is where message
observation happens — `discussion.py` must not grow a second, divergent peer
pass.

## 6. Comment → post mapping

The subtle part, and the one most likely to be got wrong.

A comment's `reply_to_top_id` is **not** the channel post id. It is the id of
the group's auto-forwarded mirror of that post. Resolution:

1. A group message is a mirror when `fwd_from.channel_post` is set **and**
   `fwd_from.from_id.channel_id` equals the collected channel id.
2. That mirror's own `id` is the value comments carry in `reply_to_top_id`.
3. `fwd_from.channel_post` is the original channel `msg_id`.

So: `comment.reply_to_top_id → mirror.id → mirror.fwd_from.channel_post → post`.

The map is an in-memory `dict[int, int]` built during the sweep from messages
as they are projected — no new table, and nothing persisted beyond the edges it
produces. A resumed run rebuilds it from `messages` rows already stored for the
group, so a comment paged in after its mirror still maps. A
comment whose `reply_to_top_id` resolves to no known mirror still gets stored
and still gets its `replied_to` edge — it just gets no `commented_on` edge.
Unmapped comments are counted and reported, never silently dropped.

**`unmapped` counts mapping *failures*, not non-candidates.** A message is a
candidate only if it carries a `reply_to_top_id`. Ordinary in-group replies
(`reply_to_top_id` NULL) and plain chatter were never comment-thread posts, so
they get their `replied_to` edge where applicable and are **not** counted as
unmapped. Counting them would put tens of thousands of ordinary messages into
the counter on the live target and destroy the only signal §13.3 relies on to
detect a mis-attribution regression. A candidate that resolves to no mirror, or
that has no resolvable author, is counted exactly once.

## 7. Edges emitted

| Subject | Predicate | Object |
|---|---|---|
| `tg:user:<author>` | `commented_on` | `tg:msg:<channel_id>/<post_id>` |
| `tg:msg:<group_id>/<comment_id>` | `replied_to` | `tg:msg:<group_id>/<parent_id>` |

Both carry `observed_at`, `tier`, and `source_raw_id` like every other edge.

**Both are idempotent on `(subject_uri, predicate, object_uri)`.**
`_write_thread_edges` re-scans every stored group row on every run — it must, so
that a comment paged in before its mirror still maps later — so an unguarded
insert would append a fresh row, with a fresh `observed_at` and the *previous*
run's `source_raw_id`, for evidence this run never gathered. On the live target
that is roughly 70k phantom rows per re-run, and it multiplies every degree
count an analyst reads off `edges`. These two predicates are structural facts
("X commented on Y"), not observations that vary over time like
`message_metrics`, so re-observing them carries no information. Skip the insert
when an identical triple already exists.
`commented_on` evidence records the comment URI it was derived from.

Anonymous/channel-authored comments produce a `tg:channel:<id>` subject rather
than `tg:user:<id>`; the predicate is unchanged. A comment with no resolvable
author (`from_id` absent) is stored and gets its `replied_to` edge, but yields
no `commented_on` — counted alongside unmapped comments, never guessed at.

## 8. `recent_repliers` backfill — zero RPC

`messageReplies.recent_repliers` arrives free inside every `Message` payload
already stored, and is currently discarded.

This runs **inside `DiscussionCollector.collect()`, as its first step**, before
preflight — so it still yields peers even when the group turns out to be
unreadable (`join_to_send`) or absent. It touches only the store:

- Scan `raw_records` where `kind='Message'` for `replies.recent_repliers`.
- Project each peer into `peers` with min-provenance
  (`seen_in_chat` = channel id, `seen_in_msg` = the post id), per the `min`
  handling already in `store/peers.py`.
- Emit `commented_on` from that peer to the post.

Handles `PeerUser` **and** `PeerChannel` — the live capture contains both.

On the current capture this yields **31 distinct commenter peers from 53 posts
before a single new RPC**, which is also the acceptance figure for its smoke
test. The field only survives on recent posts, so this complements the sweep,
never replaces it.

## 9. Guardrails

Unchanged from spec §2, restated because this is the largest RPC phase yet:

- Every RPC through the budget/guardrail module; no collector calls the gateway
  raw. `max_rpc_per_run` (default 20000) already bounds a run.
- New setting `discussion_page_budget: int = 500` — a finite per-phase page cap.
  `_HISTORY_PAGE_SIZE` is **100**, so 500 pages ≈ 50k messages: enough for the
  live target's ~35k with ~40% headroom, and not open-ended. Exceeding it is a
  `PhaseStop` with the cursor persisted, so the next run resumes rather than
  restarts — a group larger than the budget is collected across runs, never
  truncated silently.
- `FLOOD_WAIT` handled per-method by the existing gateway; `PEER_FLOOD` and
  `FROZEN_METHOD_INVALID` remain hard stops.
- Read-only. No join, no `--join` interaction, no reactions, no votes.

## 10. Ordering

`channel → history → discussion → participants → profiles → media → graph → web`
(spec §4 line 104). `discussion` requires `channel` for `linked_chat_id` and the
access hash; it does not require `history`, but runs after it so a run that
stops early has the channel's own posts first.

## 11. Testing

Fixtures derive from real captured payloads (`raw_records`) where possible, not
hand-authored shapes — the `is_self` vs `self` bug found during P0 is the
argument for this.

**Regression (must pass unmodified):** the whole existing
`tests/test_collector_history.py` and `tests/test_history_catchup.py`.

**New:**
1. Skips cleanly when `linked_chat_id` is absent/0.
2. Skips and records when the group sets `join_to_send`.
3. Skips and records when the group's `access_hash` is unavailable.
4. Builds `input_channel` from `peers` without issuing a `resolve`.
5. Comments are stored under the group's `channel_id`, not the channel's.
6. `probe_gaps=False` is honoured — no gap tombstones written for the group.
7. `reply_to_top_id` maps through a mirror to the right channel post.
8. An unmappable `reply_to_top_id` still stores the message and its
   `replied_to` edge, emits no `commented_on`, and is counted.
9. Both edge kinds are emitted with correct subject/object URIs.
10. A channel-authored (anonymous) comment yields a `tg:channel:` subject.
11. `recent_repliers` backfill projects `PeerUser` and `PeerChannel` alike.
12. The backfill runs with zero gateway calls. **`FakeGateway` has no general
    call counter today** (only `download_media_calls`); the plan must either add
    one or have this test inject a gateway whose every method raises, so the
    assertion is real rather than assumed.
13. The sweep resumes from a persisted `sync_state` cursor.
14. `discussion_page_budget` exhaustion is a `PhaseStop` with the cursor saved.

**Definition of Done** (global CLAUDE.md): unit tests are necessary, not
sufficient. The DoD report must include a smoke transcript exercising the
collector against the real stored payloads — minimally, the `recent_repliers`
backfill recovering 31 peers from the live capture, replayed the way the P1
invite-roster fix was.

## 12. Files touched

| File | Change |
|---|---|
| `src/paperboy/collectors/history.py` | Generalize `collect()`; high-water-mark state; `add_range` gated on `probe_gaps`; `PeerChannel` authors projected. Defaults preserve behaviour. **Sole owner: Task 1.** |
| `src/paperboy/collectors/discussion.py` | New |
| `src/paperboy/config.py` | `discussion_page_budget` |
| `src/paperboy/recipes.py` | Register after `history` |
| `src/paperboy/cli.py` | `--phases discussion` |
| `docs/data-model.md` | Document `commented_on`/`replied_to` subject/object shapes |
| `tests/` | Above |

## 13. Risks

1. **Generalizing a shipped collector.** The only change here that can break
   working Phase 1 code. Mitigated by the unmodified-tests contract (§5), and
   by gating the test suite before implementers fan out.
2. **Sweep size.** ~35k messages on the live target. Mitigated by the page
   budget, persisted cursor, and gap-probing off.
3. **Mirror-mapping correctness.** Silently wrong mapping would attribute
   comments to the wrong posts. Mitigated by test 8 requiring unmapped comments
   be counted and reported rather than guessed at.

## 14. Follow-ups (file as issues, do not build here)

- `channelParticipantsMentions(top_msg_id)` officially returns non-participant
  commenters — a sanctioned second discovery vector, belongs with `participants`.
- Comment media is left to the `media` collector, which walks `messages` and
  will pick the group up for free once rows exist.
- Discussion-group service messages (`messageActionChatAddUser`,
  `...JoinedByLink`) are longitudinal membership evidence; projecting them is
  its own feature.
