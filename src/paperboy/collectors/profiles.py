"""The `profiles` collector: the person layer's single enrichment authority
(spec §7). Sweeps EVERY discovered user peer — from every vector — and turns
`min` stubs into people:

1. Zero-RPC first (issue #11): forward origins and mention-name users
   referenced by stored messages get `peers` rows with provenance, so this
   very sweep can reach them.
2. Record the collecting account's own privacy posture for the run
   (`account.getPrivacy` for phone/lastseen/photo — the three keys `doctor`
   reads), so a `by_me`-degraded status is attributable to US, never misread
   as target opsec (spec §4.3).
3. Gather every `kind='user'` peer; build each one's input-user ref
   (`store.peers.input_user_ref`) — a `min` stub is reachable ONLY via
   `inputUserFromMessage` from its stored `(seen_in_chat, seen_in_msg)`.
4. Triage — batched `users.getUsers` (≤100/call, bisected on failure — plan
   D13): cheap identity for everyone, always. Writes `users` +
   `user_snapshots`, and the full object into `peers` with the stub's
   provenance preserved.
5. Full enrichment — ONLY under `--profiles`: `users.getFullUser` +
   `photos.getUserPhotos` + avatar download per user, priority admins →
   authors → commenters → others, bounded by `profile_budget`, converging
   across runs via `users.enriched_at` (plan D3). Without the flag the run
   ends after triage with a warning naming exactly what was not fetched.

Profile richness lands in `users`/`user_snapshots`/`user_photos`; `peers`
is only ever written through `upsert_peer` (never modified here).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.posture import record_privacy_posture
from paperboy.config import profile_dir
from paperboy.gateway import REPLAY_UNKNOWN_USER_KIND
from paperboy.ids import channel_uri, namespaced_kind, user_uri
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.peers import input_user_ref, upsert_full_peer
from paperboy.store.sync import record_profile_attempt, set_state
from paperboy.store.users import (
    add_user_snapshot,
    set_user_photo_sha,
    target_full_facts,
    target_user_facts,
    upsert_user,
    upsert_user_photo,
    user_photo_sha,
)
from paperboy.targets import Target

_GET_USERS_BATCH = 100
_USER_PHOTOS_LIMIT = 100
METHOD_GET_USERS = "users.getUsers"
METHOD_GET_FULL_USER = "users.getFullUser"
METHOD_GET_USER_PHOTOS = "photos.getUserPhotos"

ENRICHMENT_OFF_WARNING = (
    "profiles: triaged {n} people (basic names/handles); full enrichment (bios, photos, "
    "last-seen, …) not run — pass --profiles to enrich them (~1 getFullUser/s, bounded by "
    "--profile-budget, default {budget}/run ≈ {minutes} min)"
)


def _seconds_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


class ProfilesCollector:
    name = "profiles"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None:
            raise PhaseStop(
                "profiles skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "backfilled_peers": 0, "gathered": 0, "unresolvable": 0, "triaged": 0, "empty": 0,
            "skipped": 0, "snapshots": 0, "enriched": 0, "refreshed": 0, "fresh_skipped": 0,
            "photos": 0, "photos_empty": 0, "avatars": 0, "restricted_skipped": 0, "unavailable": 0,
        }

        # A `PhaseStop` (FLOOD_WAIT above threshold, or a second consecutive
        # one) can escape the backfill/posture calls just as readily as
        # `_triage`/`_enrich` — `record_privacy_posture` reaches a budgeted
        # `account.getPrivacy` (round-3 review) — so both live inside the try:
        # the work already stored here must still be reported and summarised
        # — `PhaseStop`'s contract (budget.py) — mirroring
        # `DiscussionCollector.collect`.
        try:
            for channel_id in self._scope_channels(ctx):
                counts["backfilled_peers"] += backfill_message_referenced_peers(
                    ctx.store, channel_id
                )
            await record_privacy_posture(ctx, self.name)

            refs = self._gather(ctx, counts)
            unusable_uris = await self._triage(ctx, refs, counts)

            if not ctx.settings.enrich_profiles:
                budget = ctx.settings.profile_budget
                ctx.log.warning(
                    ENRICHMENT_OFF_WARNING.format(
                        n=counts["triaged"], budget=budget, minutes=round(budget / 60)
                    )
                )
                record_run_event(
                    ctx.store, ctx.channel_id, self.name, "warning",
                    {
                        "code": "profiles_enrichment_off",
                        "triaged": counts["triaged"],
                        "hint": "--profiles",
                    },
                )
                self._record_summary(ctx, counts, pass_="triage_only")
                return CollectResult(name=self.name, counts=counts)

            await self._enrich(ctx, counts, unusable_uris)
        except PhaseStop as stop:
            if not stop.counts:
                # Raised below `_enrich`'s own wrap (i.e. from `_gather`/
                # `_triage`, or straight out of `budget.Budget.call`) — it
                # carries no counts of its own, so this is the only chance to
                # attach what triage actually did before it escapes.
                self._record_summary(ctx, counts, pass_="triage_only")
                raise PhaseStop(*stop.args, counts=counts) from stop
            raise
        return CollectResult(name=self.name, counts=counts)

    # ---- zero-RPC preamble ---------------------------------------------------

    @staticmethod
    def _scope_channels(ctx: CollectContext) -> list[int]:
        """The target and its linked group (if any): the channels whose stored
        messages reference the people this sweep must reach."""
        assert ctx.channel_id is not None
        row = ctx.store.conn.execute(
            "SELECT linked_chat_id FROM channels WHERE id=?", (ctx.channel_id,)
        ).fetchone()
        linked = row["linked_chat_id"] if row else None
        return [ctx.channel_id] + ([linked] if linked else [])

    # ---- gather + triage -------------------------------------------------------

    def _gather(self, ctx: CollectContext, counts: dict[str, int]) -> list[tuple[str, dict]]:
        rows = ctx.store.conn.execute(
            "SELECT uri FROM peers WHERE kind='user' ORDER BY uri"
        ).fetchall()
        refs: list[tuple[str, dict]] = []
        for row in rows:
            counts["gathered"] += 1
            ref = input_user_ref(ctx.store, row["uri"])
            if ref is None:
                counts["unresolvable"] += 1  # spec §5 case 3: recorded, never guessed
                continue
            refs.append((row["uri"], ref))
        if counts["unresolvable"]:
            ctx.log.info(
                "profiles: %d of %d users unresolvable (no full object, no usable provenance)",
                counts["unresolvable"], counts["gathered"],
            )
        return refs

    async def _triage(
        self, ctx: CollectContext, refs: list[tuple[str, dict]], counts: dict[str, int]
    ) -> set[str]:
        """Returns the URIs this run's triage proved unusable — bisected down
        to a lone `SkipAndRecord` (stale provenance), or answered `UserEmpty`
        (Telegram's definitive "this id is not visible to you") — so `_enrich`
        never spends a `getFullUser` slot on a foregone conclusion within the
        same run (round-3 and round-6 reviews)."""
        skipped_uris: set[str] = set()
        for start in range(0, len(refs), _GET_USERS_BATCH):
            batch = refs[start:start + _GET_USERS_BATCH]
            await self._triage_batch(ctx, batch, counts, skipped_uris)
        return skipped_uris

    async def _triage_batch(
        self,
        ctx: CollectContext,
        batch: list[tuple[str, dict]],
        counts: dict[str, int],
        skipped_uris: set[str],
    ) -> None:
        """One stale `from_msg` provenance fails the whole `getUsers` vector;
        bisect to isolate it rather than losing the batch (plan D13). Every
        sub-batch that DOES answer is recorded and projected before the next
        one is asked, so a `PhaseStop` (a `FLOOD_WAIT` above threshold)
        part-way through a bisection never discards responses already
        received — raw-first holds on the abort path too (round-4 review)."""
        try:
            users = await ctx.gateway.get_users([ref for _, ref in batch])
        except SkipAndRecord as exc:
            if len(batch) == 1:
                ctx.log.warning("profiles: triage skipped for %s: %s", batch[0][0], exc)
                counts["skipped"] += 1
                skipped_uris.add(batch[0][0])
                return
            mid = len(batch) // 2
            await self._triage_batch(ctx, batch[:mid], counts, skipped_uris)
            await self._triage_batch(ctx, batch[mid:], counts, skipped_uris)
            return
        for user in users:
            self._project_triaged(ctx, user, counts, skipped_uris)

    def _project_triaged(
        self, ctx: CollectContext, user: dict, counts: dict[str, int], unusable_uris: set[str]
    ) -> None:
        kind = (user.get("_") or "").lower()
        if kind not in ("user", "userempty"):
            # A non-success that must still be accounted for: in a triage-only
            # run `gathered == triaged + empty + skipped + unresolvable`
            # (`counts["skipped"]` also accumulates enrichment/photo/avatar
            # skips under --profiles, so the identity is triage-only).
            # `REPLAY_UNKNOWN_USER_KIND` is the seam's placeholder for an id
            # the original run never got an answer for (reproject D4.1's
            # analogue — the original counted it `skipped` too) and is never
            # written to raw: it is not an observation. Anything else IS
            # something the gateway returned — record it raw and say so.
            counts["skipped"] += 1
            if kind == REPLAY_UNKNOWN_USER_KIND.lower():
                return
            ctx.log.warning(
                "profiles: unexpected getUsers result %r for id %s — skipped",
                user.get("_"), user.get("id"),
            )
            ctx.store.add_raw(
                user.get("_") or "Unknown", user, ctx.tier,
                {
                    "channel_id": ctx.channel_id, "method": METHOD_GET_USERS,
                    "user_id": user.get("id"),
                },
                observed_at=ctx.clock.for_payload(user),
            )
            return
        observed_at = ctx.clock.for_payload(user)
        raw_id = ctx.store.add_raw(
            user.get("_", "User"), user, ctx.tier,
            {"channel_id": ctx.channel_id, "method": METHOD_GET_USERS, "user_id": user["id"]},
            observed_at=observed_at,
        )
        if kind == "userempty":
            counts["empty"] += 1
            unusable_uris.add(user_uri(user["id"]))
            return
        if self._project_user(ctx, user, raw_id, observed_at, METHOD_GET_USERS, counts) is not None:
            counts["triaged"] += 1

    def _project_user(
        self,
        ctx: CollectContext,
        user: dict,
        raw_id: int,
        observed_at: str,
        method: str,
        counts: dict[str, int],
        *,
        full_user: dict | None = None,
    ) -> str | None:
        """`users` + `user_snapshots` (+ the full object into `peers`, keeping
        the stub's provenance). `None` for the collecting account."""
        upsert_full_peer(ctx.store, user, raw_id, observed_at)
        uri = upsert_user(ctx.store, user, raw_id, observed_at, ctx.tier, full_user=full_user)
        if uri is None:
            return None
        bundle: dict = {"user": target_user_facts(user)}
        if full_user is not None:
            bundle["full_user"] = target_full_facts(full_user)
        if add_user_snapshot(ctx.store, uri, observed_at, ctx.tier, method, bundle, raw_id):
            counts["snapshots"] += 1
        return uri

    def _record_summary(self, ctx: CollectContext, counts: dict[str, int], *, pass_: str) -> None:
        """The run's convergence summary (spec §7.1). The enrichment POSITION
        is not stored here — it is the `profile_attempts` rotation key (plan
        D3 as amended), so an interrupted run has attempted exactly what it
        recorded and there is no cursor to corrupt. `pass` is `triage_only`
        (no `--profiles`), `initial` while any usable candidate has never
        been attempted, or `refresh` once the first pass over the population
        is complete — from then on every slot re-fetches someone."""
        population = ctx.store.conn.execute(
            "SELECT count(*) FROM peers WHERE kind='user'"
        ).fetchone()[0]
        fully = ctx.store.conn.execute(
            "SELECT count(*) FROM users WHERE enriched_at IS NOT NULL"
        ).fetchone()[0]
        set_state(ctx.store, "profiles", str(ctx.channel_id), {
            "pass": pass_, "population": population, "fully_enriched": fully,
            "enriched_this_run": counts["enriched"] + counts["refreshed"],
            "budget": ctx.settings.profile_budget,
        })

    # ---- full enrichment (--profiles) -----------------------------------------

    @staticmethod
    def _pass_label(
        ctx: CollectContext,
        candidates: list[tuple[str, int, str | None, str | None]],
        unusable_uris: set[str],
        attempted_now: set[str],
    ) -> str:
        """`initial` while a USABLE candidate (resolvable, not proved unusable
        by this run's triage) is still never-attempted; `refresh` otherwise —
        the population's first pass is complete. Defined on the attempt key,
        not on `enriched_at`: a permanently failing user has still been tried."""
        for uri, _, _, attempted_at in candidates:
            if (
                attempted_at is None
                and uri not in unusable_uris
                and uri not in attempted_now
                and input_user_ref(ctx.store, uri) is not None
            ):
                return "initial"
        return "refresh"

    async def _enrich(
        self, ctx: CollectContext, counts: dict[str, int], unusable_uris: set[str]
    ) -> None:
        """Spend `profile_budget` `getFullUser` fetches on the highest-priority
        never-enriched users, then wrap to refreshing the stalest (spec §7.1).
        A failed attempt still spends budget: the RPC was made — EXCEPT a URI
        this same run's triage already proved unusable (`unusable_uris`) —
        either its stale `(seen_in_chat, seen_in_msg)` provenance produced a
        `SkipAndRecord` at `getUsers` (the identical `CHANNEL_INVALID` would
        follow at `getFullUser`), or triage answered `UserEmpty` (Telegram's
        definitive "not visible to you", which `getFullUser` cannot improve
        on) — so retrying here would waste the budget on a foregone
        conclusion and starve every lower-priority candidate; already counted
        by triage, never spent, never double-counted (round-3/round-6 reviews).

        The whole loop is wrapped so a `PhaseStop` escaping mid-enrichment
        (a `FLOOD_WAIT` above threshold, from `get_full_user`/`get_user_photos`/
        `download_user_photo` — all reached from inside this loop) still
        records the convergence summary and carries the counts collected so
        far, rather than surfacing an empty result for a run that did real
        work (`budget.PhaseStop`'s documented contract; mirrors
        `DiscussionCollector.collect`'s `HistoryCollector` wrap).
        """
        budget = ctx.settings.profile_budget
        floor = ctx.settings.profile_refresh_after
        now = ctx.clock.now()
        spent = 0
        candidates = self._enrichment_candidates(ctx)
        attempted_now: set[str] = set()

        def label() -> str:
            return self._pass_label(ctx, candidates, unusable_uris, attempted_now)

        try:
            for uri, user_id, enriched_at, _attempted_at in candidates:
                if spent >= budget:
                    break
                if uri in unusable_uris:
                    continue
                if (
                    enriched_at is not None and floor is not None
                    and _seconds_between(enriched_at, now) < floor
                ):
                    counts["fresh_skipped"] += 1
                    continue
                ref = input_user_ref(ctx.store, uri)
                if ref is None:
                    # Already counted once in `_gather` — every `kind='user'`
                    # peer passes through both, and nothing between the two
                    # calls changes resolvability, so counting it again here
                    # would inflate the population figure past `gathered`.
                    continue
                spent += 1
                # THE attempt chokepoint (plan D3 as amended): the rotation
                # key advances here, before any outcome is known, so no arm
                # below — present or future — can spend a slot without
                # moving this user behind everyone not yet attempted. Every
                # attempt stamp is `clock.now()` (a scheduling decision, not
                # an observation): monotonic within a run live and on replay,
                # where a payload's own stamp could be far older.
                record_profile_attempt(ctx.store, uri, ctx.clock.now(), "attempted")
                attempted_now.add(uri)
                try:
                    full = await ctx.gateway.get_full_user(ref)
                except SkipAndRecord as exc:
                    ctx.log.warning("profiles: full profile skipped for %s: %s", uri, exc)
                    counts["skipped"] += 1
                    record_profile_attempt(
                        ctx.store, uri, ctx.clock.now(), "skipped", str(exc)
                    )
                    continue
                observed_at = ctx.clock.for_payload(full)
                raw_id = ctx.store.add_raw(
                    namespaced_kind("users", full, "UserFull"), full, ctx.tier,
                    {
                        "channel_id": ctx.channel_id, "user_id": user_id,
                        "method": METHOD_GET_FULL_USER,
                    },
                    observed_at=observed_at,
                )
                full_user = full.get("full_user") or {}
                user = next(
                    (u for u in (full.get("users") or []) if u.get("id") == user_id), None
                )
                if user is None:
                    # `users.UserFull` always carries the target in `users`
                    # (research Part 2 §1); a response without it is recorded
                    # raw but not projectable — counted, never guessed at.
                    counts["skipped"] += 1
                    record_profile_attempt(
                        ctx.store, uri, ctx.clock.now(), "malformed",
                        "subject missing from users vector",
                    )
                    continue
                if (user.get("_") or "").lower() != "user" or not full_user \
                        or full_user.get("id") != user["id"]:
                    # `upsert_user` raises on a non-`User` subject (`Vector<User>`'s
                    # union includes `userEmpty`) or a mismatched/absent
                    # `full_user` — a malformed envelope is symmetric with the
                    # missing-`user` case above: recorded raw, counted, never
                    # guessed at, never a crashed run.
                    counts["skipped"] += 1
                    record_profile_attempt(
                        ctx.store, uri, ctx.clock.now(), "malformed",
                        "non-User subject or missing/mismatched full_user",
                    )
                    continue
                for chat in full.get("chats") or []:
                    # e.g. the personal channel (`personal_channel_id`): a
                    # full Channel object — a real pivot, worth a peer row.
                    # `Vector<Chat>`'s union also legally carries `chatEmpty`
                    # (id only) and `chatForbidden` (id + title) — nothing to
                    # project, and a full observation with no richness would
                    # NULL a known peer's identity under `upsert_peer`'s
                    # recency rule — and `channelForbidden`, which DOES carry
                    # id + access_hash + title (a channel we are banned from):
                    # worth a row when the channel is new to us, fill-only
                    # otherwise (it has no `username` to offer).
                    chat_kind = (chat.get("_") or "").lower()
                    if chat_kind in ("chatempty", "chatforbidden"):
                        continue
                    if chat_kind == "channelforbidden" and ctx.store.conn.execute(
                        "SELECT 1 FROM peers WHERE uri=?", (channel_uri(chat["id"]),)
                    ).fetchone() is not None:
                        continue
                    upsert_full_peer(ctx.store, chat, raw_id, observed_at)
                if self._project_user(
                    ctx, user, raw_id, observed_at, METHOD_GET_FULL_USER, counts,
                    full_user=full_user,
                ) is None:
                    record_profile_attempt(ctx.store, uri, ctx.clock.now(), "not_projected")
                    continue
                counts["refreshed" if enriched_at is not None else "enriched"] += 1
                record_profile_attempt(ctx.store, uri, ctx.clock.now(), "enriched")
                await self._photos(ctx, uri, user_id, ref, counts)
        except PhaseStop as stop:
            self._record_summary(ctx, counts, pass_=label())
            raise PhaseStop(*stop.args, counts=counts) from stop
        if spent >= budget:
            ctx.log.info(
                "profiles: getFullUser budget (%d) spent this run; re-run to keep converging",
                budget,
            )
        self._record_summary(ctx, counts, pass_=label())

    def _enrichment_candidates(
        self, ctx: CollectContext
    ) -> list[tuple[str, int, str | None, str | None]]:
        """Every discovered user, in spend order (spec §7/§7.1), keyed on the
        ROTATION key `profile_attempts.attempted_at` (plan D3 as amended) —
        `users.enriched_at` plays no part in the ORDER: it moves only on
        success, so any ordering that partitions on it pins a permanently-
        failing user to the head of the queue (found twice: first in the
        refresh pass, then — with `enriched_at IS NOT NULL` still the leading
        term — for a user never enriched at all, which starved the refresh
        pass once every fresh candidate was used up).

        1. never attempted — admins → authors → commenters → others OF THIS
           TARGET (the rank arms are scoped to `ctx.channel_id` and its
           linked group; a sibling target sharing this profile DB ranks as
           "other", never pre-empting this target's own people), then `uri`
           for determinism (replay must make the same choices);
        2. everyone else strictly least-recently-attempted first, enriched or
           not — so a user whose fetch keeps failing costs exactly one slot
           per lap and can never block the refresh wrap, and with no failures
           the wrap is exactly "stalest enrichment first" (every successful
           enrichment is also an attempt).

        A user with no `users` row yet (its triage batch failed) still gets a
        turn: `getFullUser` triages as a side effect."""
        assert ctx.channel_id is not None
        scope = self._scope_channels(ctx)
        group_id = scope[1] if len(scope) > 1 else None
        rows = ctx.store.conn.execute(
            """
            SELECT p.uri AS uri, p.id AS id, u.enriched_at AS enriched_at,
              a.attempted_at AS attempted_at,
              CASE
                WHEN EXISTS (SELECT 1 FROM participants pa WHERE pa.uri = p.uri
                             AND pa.status IN ('admin', 'creator')
                             AND (pa.group_id = ? OR (? IS NOT NULL AND pa.group_id = ?))
                             ) THEN 0
                WHEN EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                             AND m.channel_id = ?) THEN 1
                WHEN ? IS NOT NULL AND EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                                               AND m.channel_id = ?) THEN 2
                ELSE 3
              END AS rank
            FROM peers p
              LEFT JOIN users u ON u.uri = p.uri
              LEFT JOIN profile_attempts a ON a.uri = p.uri
            WHERE p.kind = 'user'
            ORDER BY (a.attempted_at IS NOT NULL),
                     COALESCE(a.attempted_at, u.enriched_at),
                     rank, p.uri
            """,
            (ctx.channel_id, group_id, group_id, ctx.channel_id, group_id, group_id),
        ).fetchall()
        return [(r["uri"], r["id"], r["enriched_at"], r["attempted_at"]) for r in rows]

    async def _photos(
        self, ctx: CollectContext, uri: str, user_id: int, ref: dict, counts: dict[str, int]
    ) -> None:
        """The target's own dated avatar history (research Part 2 §5), then
        each photo's bytes through the media/custody path (plan D12)."""
        try:
            photos = await ctx.gateway.get_user_photos(
                ref, offset=0, max_id=0, limit=_USER_PHOTOS_LIMIT
            )
        except SkipAndRecord as exc:
            ctx.log.warning("profiles: photo history skipped for %s: %s", uri, exc)
            counts["skipped"] += 1
            return
        observed_at = ctx.clock.for_payload(photos)
        raw_id = ctx.store.add_raw(
            namespaced_kind("photos", photos, "Photos"), photos, ctx.tier,
            {"channel_id": ctx.channel_id, "user_id": user_id, "method": METHOD_GET_USER_PHOTOS},
            observed_at=observed_at,
        )
        row = ctx.store.conn.execute(
            "SELECT restriction_json FROM users WHERE uri=?", (uri,)
        ).fetchone()
        restricted = bool(row and row["restriction_json"])
        media_root = profile_dir(ctx.settings, ctx.profile) / "media"
        for photo in photos.get("photos") or []:
            if (photo.get("_") or "").lower() != "photo":
                counts["photos_empty"] += 1  # `photoEmpty`: counted, never a row
                continue
            upsert_user_photo(ctx.store, uri, photo, observed_at, raw_id)
            counts["photos"] += 1
            if restricted:
                # The "don't download porno/illegal-flagged by default" rule
                # (spec §9): the history is recorded, the bytes are not fetched.
                counts["restricted_skipped"] += 1
                continue
            if user_photo_sha(ctx.store, uri, photo["id"]) is not None:
                continue  # content-addressed and already on disk: never re-fetched
            await self._download_avatar(ctx, uri, user_id, photo, media_root, counts)

    async def _download_avatar(
        self,
        ctx: CollectContext,
        uri: str,
        user_id: int,
        photo: dict,
        media_root,
        counts: dict[str, int],
    ) -> None:
        try:
            data = await ctx.gateway.download_user_photo(photo)
        except SkipAndRecord as exc:
            ctx.log.warning("profiles: avatar %s skipped for %s: %s", photo["id"], uri, exc)
            counts["skipped"] += 1
            return
        if data is None:
            # Server-side-unavailable (e.g. a stale file reference) rather
            # than a raised `SkipAndRecord` — still worth a log line AND a
            # count so the outcome isn't invisible, matching `media.py`'s
            # identical `download_media` -> `None` case.
            ctx.log.warning(
                "profiles: avatar %s unavailable for %s (download returned no bytes)",
                photo["id"], uri,
            )
            counts["unavailable"] += 1
            return
        sha = hashlib.sha256(data).hexdigest()
        path = media_root / sha[:2] / f"{sha}.jpg"  # Telegram re-encodes avatars as JPEG
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        raw_payload = {
            "sha256": sha, "path": str(path), "size": len(data),
            "user_uri": uri, "photo_id": photo["id"],
        }
        downloaded_at = ctx.clock.for_payload(raw_payload)
        ctx.store.conn.execute(
            "INSERT INTO media (sha256, message_uri, kind, mime_type, size, file_name, "
            "attributes_json, path, downloaded_at) "
            "VALUES (?, NULL, 'avatar', 'image/jpeg', ?, NULL, NULL, ?, ?) "
            "ON CONFLICT(sha256) DO NOTHING",
            (sha, len(data), str(path), downloaded_at),
        )
        ctx.store.conn.execute(
            "INSERT INTO custody_log (path, sha256, recorded_at, source_message_uri) "
            "VALUES (?, ?, ?, NULL)",
            (str(path), sha, downloaded_at),
        )
        ctx.store.add_raw(
            "AvatarDownload", raw_payload, ctx.tier,
            {"channel_id": ctx.channel_id, "user_id": user_id, "photo_id": photo["id"]},
            observed_at=downloaded_at,
        )
        set_user_photo_sha(ctx.store, uri, photo["id"], sha)
        counts["avatars"] += 1
