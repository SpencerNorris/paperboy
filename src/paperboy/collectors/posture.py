"""The collecting account's own privacy posture, recorded ONCE per run (spec
§4.3): `account.getPrivacy` for `phone`/`lastseen`/`photo` — the three keys
`doctor` reads. It is what makes a `by_me`-degraded `userStatus*` (a coarse
bucket because OUR privacy hides exact status) attributable to us, never
misread as target opsec. Both person-layer collectors call it; the first one
in a run records it (raw `account.PrivacyRules` per key + a `run_events`
`privacy_posture` row), later calls in the same run are no-ops."""

from __future__ import annotations

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.ids import namespaced_kind
from paperboy.store.events import record_run_event
from paperboy.store.sync import get_state, set_state

PRIVACY_KEYS = ("phone", "lastseen", "photo")


async def record_privacy_posture(ctx: CollectContext, phase: str) -> bool:
    """Returns whether THIS call recorded the posture (False: already
    recorded earlier in the same run)."""
    marker = get_state(ctx.store, "account", "privacy_posture") or {}
    if ctx.store.run_id is not None and marker.get("run_id") == ctx.store.run_id:
        return False
    posture: dict[str, object] = {}
    for key in PRIVACY_KEYS:
        try:
            rules = await ctx.gateway.get_privacy(key)
        except SkipAndRecord as exc:
            posture[key] = {"unavailable": str(exc)}
            continue
        observed_at = ctx.clock.for_payload(rules)
        ctx.store.add_raw(
            namespaced_kind("account", rules, "PrivacyRules"), rules, "self", {"key": key},
            observed_at=observed_at,
        )
        posture[key] = [(rule.get("_") or "") for rule in rules.get("rules") or []]
    record_run_event(ctx.store, ctx.channel_id, phase, "privacy_posture", posture)
    set_state(
        ctx.store, "account", "privacy_posture",
        {"run_id": ctx.store.run_id, "posture": posture},
    )
    return True
