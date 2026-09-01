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

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.posture import record_privacy_posture
from paperboy.ids import channel_uri, user_uri
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.peers import input_user_ref, upsert_peer
from paperboy.store.sync import set_state
from paperboy.store.users import (
    add_user_snapshot,
    target_full_facts,
    target_user_facts,
    upsert_user,
)
from paperboy.targets import Target

_GET_USERS_BATCH = 100
METHOD_GET_USERS = "users.getUsers"
METHOD_GET_FULL_USER = "users.getFullUser"

ENRICHMENT_OFF_WARNING = (
    "profiles: triaged {n} people (basic names/handles); full enrichment (bios, photos, "
    "last-seen, …) not run — pass --profiles to enrich them (~1 getFullUser/s, bounded by "
    "--profile-budget, default {budget}/run ≈ {minutes} min)"
)


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
        the stored provenance through instead."""
        kind = (obj.get("_") or "").lower()
        uri = user_uri(obj["id"]) if kind.startswith("user") else channel_uri(obj["id"])
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
        self._record_summary(ctx, counts, pass_="initial")
