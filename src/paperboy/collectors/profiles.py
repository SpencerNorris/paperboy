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
from paperboy.ids import namespaced_kind
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.peers import classify_peer, input_user_ref, upsert_peer
from paperboy.store.sync import set_state
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
            "photos": 0, "avatars": 0, "restricted_skipped": 0,
        }
        for channel_id in self._scope_channels(ctx):
            counts["backfilled_peers"] += backfill_message_referenced_peers(ctx.store, channel_id)
        await record_privacy_posture(ctx, self.name)

        # A `PhaseStop` (FLOOD_WAIT above threshold, or a second consecutive
        # one) can escape `_triage` or `_enrich` — `budget.Budget.call` raises
        # it directly, with no `counts`, and only `_enrich` attaches its own
        # (below) before re-raising. Either way the work already stored here
        # must still be reported and summarised — `PhaseStop`'s contract
        # (budget.py) — mirroring `DiscussionCollector.collect`.
        try:
            refs = self._gather(ctx, counts)
            await self._triage(ctx, refs, counts)

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

            await self._enrich(ctx, counts)
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
    ) -> None:
        for start in range(0, len(refs), _GET_USERS_BATCH):
            batch = refs[start:start + _GET_USERS_BATCH]
            for user in await self._get_users_resilient(ctx, batch, counts):
                self._project_triaged(ctx, user, counts)

    async def _get_users_resilient(
        self, ctx: CollectContext, batch: list[tuple[str, dict]], counts: dict[str, int]
    ) -> list[dict]:
        """One stale `from_msg` provenance fails the whole `getUsers` vector;
        bisect to isolate it rather than losing the batch (plan D13)."""
        try:
            return await ctx.gateway.get_users([ref for _, ref in batch])
        except SkipAndRecord as exc:
            if len(batch) == 1:
                ctx.log.warning("profiles: triage skipped for %s: %s", batch[0][0], exc)
                counts["skipped"] += 1
                return []
            mid = len(batch) // 2
            head = await self._get_users_resilient(ctx, batch[:mid], counts)
            tail = await self._get_users_resilient(ctx, batch[mid:], counts)
            return head + tail

    def _project_triaged(self, ctx: CollectContext, user: dict, counts: dict[str, int]) -> None:
        kind = (user.get("_") or "").lower()
        if kind not in ("user", "userempty"):
            # `ReplayUnknownUser`: the original run never observed this id —
            # nothing to project (reproject D4.1's analogue).
            return
        observed_at = ctx.clock.for_payload(user)
        raw_id = ctx.store.add_raw(
            user.get("_", "User"), user, ctx.tier,
            {"channel_id": ctx.channel_id, "method": METHOD_GET_USERS, "user_id": user["id"]},
            observed_at=observed_at,
        )
        if kind == "userempty":
            counts["empty"] += 1
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
        self._upsert_peer_keeping_provenance(ctx, user, raw_id, observed_at)
        uri = upsert_user(ctx.store, user, raw_id, observed_at, ctx.tier, full_user=full_user)
        if uri is None:
            return None
        bundle: dict = {"user": target_user_facts(user)}
        if full_user is not None:
            bundle["full_user"] = target_full_facts(full_user)
        if add_user_snapshot(ctx.store, uri, observed_at, ctx.tier, method, bundle, raw_id):
            counts["snapshots"] += 1
        return uri

    @staticmethod
    def _upsert_peer_keeping_provenance(
        ctx: CollectContext, obj: dict, raw_id: int, observed_at: str
    ) -> str | None:
        """`upsert_peer` for a FULL object returned by a profile RPC. A full
        observation carrying no provenance of its own would — correctly, by
        the recency rule — overwrite the stub's `seen_in_chat`/`seen_in_msg`
        with NULLs, losing the only path back to `inputUserFromMessage`. Pass
        the stored provenance through instead.

        The URI must be derived exactly the way `upsert_peer` itself derives
        it — via `classify_peer` — not re-hand-rolled: a `Chat`/`ChatForbidden`/
        `ChatEmpty` object (a legal member of `users.UserFull.chats`, a
        `Vector<Chat>`) classifies as `chat`, not `channel`; a hand-rolled
        `user_uri(...) if kind.startswith("user") else channel_uri(...)`
        branch reads/writes the wrong peer row for one (round-2 review).
        """
        _, uri, _ = classify_peer(obj)
        row = ctx.store.conn.execute(
            "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
        ).fetchone()
        return upsert_peer(
            ctx.store, obj, raw_id, observed_at,
            seen_in_chat=row["seen_in_chat"] if row else None,
            seen_in_msg=row["seen_in_msg"] if row else None,
        )

    def _record_summary(self, ctx: CollectContext, counts: dict[str, int], *, pass_: str) -> None:
        """The run's convergence summary (spec §7.1). The enrichment POSITION
        is derived from `users.enriched_at`, not stored here — an interrupted
        run has enriched exactly those it wrote, so there is no cursor to
        corrupt (plan D3)."""
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

    async def _enrich(self, ctx: CollectContext, counts: dict[str, int]) -> None:
        """Spend `profile_budget` `getFullUser` fetches on the highest-priority
        never-enriched users, then wrap to refreshing the stalest (spec §7.1).
        A failed attempt still spends budget: the RPC was made.

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
        pass_ = "initial"
        try:
            for uri, user_id, enriched_at in self._enrichment_candidates(ctx):
                if spent >= budget:
                    break
                if enriched_at is not None:
                    pass_ = "refresh"
                    if floor is not None and _seconds_between(enriched_at, now) < floor:
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
                try:
                    full = await ctx.gateway.get_full_user(ref)
                except SkipAndRecord as exc:
                    ctx.log.warning("profiles: full profile skipped for %s: %s", uri, exc)
                    counts["skipped"] += 1
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
                    continue
                if not full_user or full_user.get("id") != user["id"]:
                    # `upsert_user` raises on a mismatched/absent `full_user`
                    # (store/users.py) — a malformed envelope is symmetric
                    # with the missing-`user` case above: recorded raw,
                    # counted, never guessed at, never a crashed run.
                    counts["skipped"] += 1
                    continue
                for chat in full.get("chats") or []:
                    # e.g. the personal channel (`personal_channel_id`): a
                    # full Channel object — a real pivot, worth a peer row.
                    self._upsert_peer_keeping_provenance(ctx, chat, raw_id, observed_at)
                if self._project_user(
                    ctx, user, raw_id, observed_at, METHOD_GET_FULL_USER, counts,
                    full_user=full_user,
                ) is None:
                    continue
                counts["refreshed" if enriched_at is not None else "enriched"] += 1
                await self._photos(ctx, uri, user_id, ref, counts)
        except PhaseStop as stop:
            self._record_summary(ctx, counts, pass_=pass_)
            raise PhaseStop(*stop.args, counts=counts) from stop
        if spent >= budget:
            ctx.log.info(
                "profiles: getFullUser budget (%d) spent this run; re-run to keep converging",
                budget,
            )
        self._record_summary(ctx, counts, pass_=pass_)

    def _enrichment_candidates(self, ctx: CollectContext) -> list[tuple[str, int, str | None]]:
        """Every discovered user, in spend order (spec §7/§7.1): never-enriched
        first — admins → authors → commenters → others, then `uri` for
        determinism (replay must make the same choices) — then already-
        enriched users stalest first (the refresh wrap). A user with no
        `users` row yet (its triage batch failed) still gets a turn:
        `getFullUser` triages as a side effect."""
        assert ctx.channel_id is not None
        scope = self._scope_channels(ctx)
        group_id = scope[1] if len(scope) > 1 else None
        rows = ctx.store.conn.execute(
            """
            SELECT p.uri AS uri, p.id AS id, u.enriched_at AS enriched_at,
              CASE
                WHEN EXISTS (SELECT 1 FROM participants pa WHERE pa.uri = p.uri
                             AND pa.status IN ('admin', 'creator')) THEN 0
                WHEN EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                             AND m.channel_id = ?) THEN 1
                WHEN ? IS NOT NULL AND EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                                               AND m.channel_id = ?) THEN 2
                ELSE 3
              END AS rank
            FROM peers p LEFT JOIN users u ON u.uri = p.uri
            WHERE p.kind = 'user'
            ORDER BY (u.enriched_at IS NOT NULL), u.enriched_at, rank, p.uri
            """,
            (ctx.channel_id, group_id, group_id),
        ).fetchall()
        return [(r["uri"], r["id"], r["enriched_at"]) for r in rows]

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
                continue  # PhotoEmpty
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
            # than a raised `SkipAndRecord` — still worth a log line so the
            # outcome isn't invisible, matching `media.py`'s identical
            # `download_media` -> `None` case.
            ctx.log.warning(
                "profiles: avatar %s unavailable for %s (download returned no bytes)",
                photo["id"], uri,
            )
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
