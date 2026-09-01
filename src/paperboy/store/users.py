"""Person projection: current profile state (`users`) + append-only
observation log (`user_snapshots`) + dated avatar history (`user_photos`).

Profile richness lives HERE, never in `peers` (spec §4): `peers` stays the
min-provenance stub table with its own merge lattice (ADR-0005 §6, open
residuals #38/#39), and this module never touches it.

Tri-state (spec §4.3, encoded per plan D2): every privacy-gated field carries
`{"state": present | absent | hidden_from_you, ...}` in `field_states_json`.
Telegram enforces privacy by OMISSION — there is no privacy-denial error —
so plain absence never proves "not set": `absent` is the honest record for
"not on the wire, no disambiguator". `hidden_from_you` is written only with
a machine-readable proof (`fallback_photo` present while `profile_photo` is
absent; `private_forward_name` present). A user status keeps `present` with
its granularity; a coarse bucket's `coarse_cause` is `self_privacy` when
`by_me` is set — OUR account's privacy is degrading the data, not the
target's opsec (research §6).

Merge rules follow research §8.7 for `min` objects (a min observation never
clobbers full identity; status only if the cached status is empty; photo only
with `apply_min_photo`) and ADR-0005 §6's composed richness ∘ recency lattice
for full ones, done in Python rather than one giant CASE statement so the
cells stay readable. Two independent recency benchmarks apply: the TRIAGE
lattice (identity, status, the bare-`User` avatar, the triage bot flags) is
judged against `last_seen`; the ENRICHMENT lattice (`about`, `birthday`, the
full-level avatar truth, the `UserFull` bot surface, the full-level field
states) is judged against `enriched_at` — so `enriched_at` moves exactly when
those columns do, never alone, and an out-of-order full observation can
neither clobber a newer enrichment nor be lost behind a newer triage.

The avatar is the one field both levels observe; it is triage-owned only
until the row's first enrichment, then enrichment-owned (see `upsert_user`),
which keeps the projection order-independent ACROSS the two levels. Within
the triage level, the `min`/full composition keeps ADR-0005 §6's documented
richness-vs-recency residual (#38's shape): a `min` `apply_min_photo` write
and an older full `photo` can still land in either order until the row is
enriched. `bot_json` is level-keyed (`{"user": {...}, "full": {...}}`), each
level replaced by its own observations, so cleared flags never linger.

Facts about US (`contact`, `bot_can_edit`, `blocked`, `common_chats_count`,
...) are stripped at ONE chokepoint — `target_user_facts` /
`target_full_facts` — and every column, including `bot_json`, is derived
from those filtered dicts, so the guardrail has a single enforcement point.
"""

from __future__ import annotations

import hashlib
import json

from paperboy.ids import iso_or_none, primary_username, user_uri
from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self

# `User` flags that are facts about the TARGET (kept in `users.flags_json`).
_TARGET_FLAG_KEYS = (
    "deleted", "bot", "verified", "restricted", "support", "scam", "fake", "premium",
    "stories_unavailable", "contact_require_premium",
)
# `User` fields that describe US or our relationship, never the target
# (research §2a/§2b, §8.7): never ingested.
_USER_SELF_FACTS = frozenset({
    "is_self", "contact", "mutual_contact", "attach_menu_enabled", "bot_can_edit",
    "close_friend", "stories_hidden", "min", "apply_min_photo",
})
# `UserFull` fields that describe OUR side (spec §4.3's four, plus every
# other SELF/REL field in research §3a/§3b): never ingested as target data.
_FULL_SELF_FACTS = frozenset({
    "common_chats_count", "blocked", "personal_photo", "note", "settings",
    "notify_settings", "folder_id", "pinned_msg_id", "ttl_period", "theme", "wallpaper",
    "wallpaper_overridden", "translations_disabled", "blocked_my_stories_from",
    "has_scheduled", "can_pin_message", "sponsored_enabled", "stars_my_pending_rating",
    "stars_my_pending_rating_date", "noforwards_my_enabled", "display_gifts_button",
    "business_greeting_message", "business_away_message", "can_view_revenue",
    "bot_can_manage_emoji_status",
})
_STATUS_KINDS = {
    "userstatusonline": "online", "userstatusoffline": "offline",
    "userstatusrecently": "recently", "userstatuslastweek": "last_week",
    "userstatuslastmonth": "last_month", "userstatusempty": "empty",
}
_COARSE_STATUS = {"recently", "last_week", "last_month"}
# Field-state keys only a `UserFull` can observe — plus `photo`, which only a
# `UserFull` can DISAMBIGUATE — governed by the enrichment benchmark.
_FULL_STATE_KEYS = ("photo", "about", "birthday", "forwards", "stories")


def _kind(obj: dict | None) -> str:
    return ((obj or {}).get("_") or "").lower()


def _facts(obj: dict, exclude: frozenset[str]) -> dict:
    """`obj` minus the discriminator, the excluded keys, and empty values
    (`None`, `""`, `[]`) — absence is recorded by `field_states`, not by a
    sea of nulls in the snapshot bundle."""
    return {
        k: v for k, v in obj.items()
        if k != "_" and k not in exclude and v is not None and v != "" and v != []
    }


def target_user_facts(user: dict) -> dict:
    return _facts(user, _USER_SELF_FACTS)


def target_full_facts(full_user: dict) -> dict:
    return _facts(full_user, _FULL_SELF_FACTS)


def field_states(user: dict, full_user: dict | None = None) -> dict[str, dict]:
    """The tri-state map for one observation. Keys observable at triage level
    (a bare `User`): `phone`, `photo`, `status`. With a `UserFull`: also
    `about`, `birthday`, `forwards`, `stories`, and `photo` gains its
    disambiguator."""
    states: dict[str, dict] = {}

    phone = user.get("phone")
    if phone:
        states["phone"] = {"state": "present"}
    elif user.get("min") and phone == "":
        # flags.4 set with "" is a real min wire state (research §8.2):
        # test non-empty, never presence.
        states["phone"] = {"state": "absent", "why": "min_empty_string"}
    else:
        states["phone"] = {"state": "absent"}

    if full_user is not None:
        # `Photo` is a union that includes `photoEmpty` — only a real `photo`
        # constructor is evidence of anything (mirrors the triage check).
        if _kind(full_user.get("profile_photo")) == "photo":
            states["photo"] = {"state": "present"}
        elif _kind(full_user.get("fallback_photo")) == "photo":
            states["photo"] = {"state": "hidden_from_you", "why": "fallback_photo"}
        else:
            states["photo"] = {"state": "absent"}
    else:
        photo = user.get("photo") or {}
        if _kind(photo) == "userprofilephoto" and not photo.get("personal"):
            states["photo"] = {"state": "present"}
        elif photo.get("personal"):
            # A photo WE set for them shadows their real avatar in `user.photo`
            # (research §8.5) — zero information about the target.
            states["photo"] = {"state": "absent", "why": "personal_photo_shadows"}
        else:
            states["photo"] = {"state": "absent"}

    status = user.get("status") or {}
    status_kind = _STATUS_KINDS.get(_kind(status))
    if status_kind in ("online", "offline"):
        states["status"] = {"state": "present", "granularity": "exact"}
    elif status_kind in _COARSE_STATUS:
        states["status"] = {
            "state": "present", "granularity": "coarse",
            "coarse_cause": "self_privacy" if status.get("by_me") else "target_privacy",
        }
    else:
        states["status"] = {"state": "absent"}

    if full_user is not None:
        states["about"] = {"state": "present" if full_user.get("about") else "absent"}
        states["birthday"] = {"state": "present" if full_user.get("birthday") else "absent"}
        if full_user.get("private_forward_name"):
            states["forwards"] = {"state": "hidden_from_you", "why": "private_forward_name"}
        else:
            states["forwards"] = {"state": "absent"}
        if full_user.get("stories"):
            states["stories"] = {"state": "present"}
        elif user.get("stories_unavailable"):
            states["stories"] = {"state": "absent", "why": "stories_unavailable"}
        else:
            states["stories"] = {"state": "absent"}
    return states


def merge_field_states(existing: dict, incoming: dict, *, full: bool) -> dict:
    """Newest observation wins per key it can observe — except that a stored
    `hidden_from_you` proof is never revised by a TRIAGE-level observation
    (`full=False`), whatever it says: a bare `User` cannot disambiguate, and
    for a hidden photo it even shows the public fallback decoy as an ordinary
    avatar. Only a FULL observation (`full=True`) revises a proof."""
    merged = dict(existing)
    for key, state in incoming.items():
        if not full and merged.get(key, {}).get("state") == "hidden_from_you":
            continue
        merged[key] = state
    return merged


def _bot_json(bot: dict) -> str | None:
    """`{"user": {...}, "full": {...}}` minus empty levels; `None` when nothing
    bot-shaped was ever observed (a non-bot)."""
    levels = {level: facts for level, facts in bot.items() if facts}
    return dumps(levels) if levels else None


def _bot_facts(facts: dict) -> dict:
    """The bot-only surface of an ALREADY-FILTERED facts dict — the one place
    `bot_*` keys are selected, downstream of the SELF/REL exclusion, so
    `bot_can_edit` (we own it) / `bot_can_manage_emoji_status` (we allowed
    it) can never leak into `users.bot_json`."""
    return {k: v for k, v in facts.items() if k.startswith("bot_")}


def _triage_columns(user: dict) -> tuple[dict, dict]:
    """Columns a bare `User` can populate, plus the triage-level bot facts —
    every one read from `target_user_facts(user)`, the chokepoint that strips
    facts-about-us (research §2a/§2b, spec §4.3), never from `user` itself."""
    facts = target_user_facts(user)
    status = facts.get("status") or {}
    status_kind = _STATUS_KINDS.get(_kind(status))
    if status_kind == "online":
        status_value = iso_or_none(status.get("expires"))
    elif status_kind == "offline":
        status_value = iso_or_none(status.get("was_online"))
    else:
        status_value = None

    photo = facts.get("photo") or {}
    photo_ref = None
    if _kind(photo) == "userprofilephoto" and not photo.get("personal"):
        photo_ref = dumps(
            {k: photo.get(k) for k in ("photo_id", "dc_id", "has_video", "stripped_thumb")}
        )

    emoji = facts.get("emoji_status") or {}
    emoji_status = dumps(emoji) if emoji and _kind(emoji) != "emojistatusempty" else None
    color = {k: facts[k] for k in ("color", "profile_color") if facts.get(k)}
    usernames = [
        {k: e.get(k) for k in ("username", "editable", "active")}
        for e in (facts.get("usernames") or [])
    ]
    flags = {k: facts[k] for k in _TARGET_FLAG_KEYS if facts.get(k)}
    restriction = facts.get("restriction_reason") or None
    columns = {
        "id": facts["id"],
        "access_hash": facts.get("access_hash"),
        "username": primary_username(facts),
        "usernames_json": dumps(usernames) if usernames else None,
        "first_name": facts.get("first_name"),
        "last_name": facts.get("last_name"),
        "phone": facts.get("phone"),  # `_facts` already dropped the min "" wire state
        "emoji_status": emoji_status,
        "color_json": dumps(color) if color else None,
        "status_kind": status_kind,
        "status_value": status_value,
        "photo_ref": photo_ref,
        "restriction_json": dumps(restriction) if restriction else None,
        "flags_json": dumps(flags) if flags else None,
    }
    return columns, (_bot_facts(facts) if facts.get("bot") else {})


def _full_columns(full_user: dict) -> tuple[dict, dict]:
    """Columns only a `UserFull` can populate, plus the full-level bot surface
    (bot_info, bot_group/broadcast_admin_rights, bot_verification,
    bot_manager_id, ...) — derived from `target_full_facts(full_user)`. The
    full level is the truth about the avatar: the target's real
    `profile_photo`, or nothing — never the fallback decoy that `user.photo`
    shows a privacy-excluded viewer, and never a personal photo of ours."""
    facts = target_full_facts(full_user)
    birthday = facts.get("birthday")
    profile_photo = facts.get("profile_photo")
    if _kind(profile_photo) != "photo":
        profile_photo = None  # `photoEmpty` (or a malformed dict) is not an avatar
    columns = {
        "about": facts.get("about"),
        "birthday": (
            dumps({k: birthday.get(k) for k in ("day", "month", "year")}) if birthday else None
        ),
        "photo_ref": (
            dumps({
                "photo_id": profile_photo.get("id"), "dc_id": profile_photo.get("dc_id"),
                "has_video": bool(profile_photo.get("video_sizes")), "stripped_thumb": None,
            })
            if profile_photo else None
        ),
    }
    return columns, _bot_facts(facts)


def upsert_user(
    store: Store,
    user: dict,
    source_raw_id: int,
    observed_at: str,
    tier: str,
    *,
    full_user: dict | None = None,
) -> str | None:
    """Project one `User` (plus its `UserFull`, when this observation came
    from `users.getFullUser`) into `users`; returns the URI, or `None` for the
    collecting account (never a subject, #12).

    Two lattices, two benchmarks (module docstring): (i) the TRIAGE lattice
    — identity/status/bot-flag columns from the bare `User` — moves on
    `last_seen` recency composed with min/full richness (ADR-0005 §6,
    research §8.7); (ii) the ENRICHMENT lattice — `about`, `birthday`, the
    avatar, the `UserFull` bot surface, the full-level field states — moves
    iff this full observation is at least as new as the last one applied
    (`enriched_at`), and `enriched_at` moves WITH those columns, never alone.

    The avatar (`photo_ref` + `field_states.photo`) is the one thing both
    levels can observe; to stay order-independent it belongs to exactly one
    lattice at a time: a triage or `min` observation may set it only while
    the row has never been enriched, and once a `UserFull` has been applied
    only a newer `UserFull` moves it (a bare `User` shows a privacy-excluded
    viewer the fallback decoy, so the full level is the only honest source).
    `bot_json` is level-keyed — `{"user": {...}, "full": {...}}` — and each
    level is REPLACED by its own observations, so a flag the target cleared
    does not linger as a stale current-state fact.

    `full_user` must describe `user` (`users.userFull.users` can carry more
    than the subject); a mismatched pair is a caller bug and raises.
    """
    kind = _kind(user)
    if not kind.startswith("user") or kind == "userempty":
        raise ValueError(f"not a User object: {user.get('_')!r}")
    if full_user is not None and full_user.get("id") != user["id"]:
        raise ValueError(
            f"full_user is for user {full_user.get('id')!r}, not {user['id']!r} — "
            "pass the `users` vector entry that matches `full_user.id`"
        )
    uri = user_uri(user["id"])
    if is_self(store, uri):
        return None
    incoming_min = bool(user.get("min"))
    triage, triage_bot = _triage_columns(user)
    triage_states = field_states(user)
    full_cols: dict | None = None
    full_bot: dict = {}
    full_states: dict = {}
    if full_user is not None:
        full_cols, full_bot = _full_columns(full_user)
        observed = field_states(user, full_user)
        full_states = {key: observed[key] for key in _FULL_STATE_KEYS}

    existing = store.conn.execute("SELECT * FROM users WHERE uri=?", (uri,)).fetchone()
    if existing is None:
        cols = dict(triage)
        states = dict(triage_states)
        bot = {"user": triage_bot}
        if full_cols is not None:
            cols.update(full_cols)
            states.update(full_states)
            bot["full"] = full_bot
        cols.update({
            "uri": uri, "tier": tier, "is_min": int(incoming_min),
            "bot_json": _bot_json(bot),
            "field_states_json": dumps(states),
            "enriched_at": observed_at if full_cols is not None else None,
            "source_raw_id": source_raw_id, "first_seen": observed_at, "last_seen": observed_at,
        })
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        store.conn.execute(f"INSERT INTO users ({names}) VALUES ({marks})", tuple(cols.values()))
        return uri

    updates: dict = {}
    states = json.loads(existing["field_states_json"] or "{}")
    bot = json.loads(existing["bot_json"]) if existing["bot_json"] else {}
    enriched = existing["enriched_at"] is not None

    # (i) The triage lattice — benchmark `last_seen`, composed with richness.
    newer = observed_at >= existing["last_seen"]
    if incoming_min and not existing["is_min"]:
        # research §8.7: a min object never clobbers a full row's identity;
        # status only if the cached status is empty, photo only with
        # `apply_min_photo` (and only while the avatar is still triage-owned)
        # — both recency-gated, both mirrored in `field_states` (D2).
        applied: dict = {}
        if newer and existing["status_kind"] in (None, "empty") and triage["status_kind"]:
            updates["status_kind"] = triage["status_kind"]
            updates["status_value"] = triage["status_value"]
            applied["status"] = triage_states["status"]
        if newer and user.get("apply_min_photo") and triage["photo_ref"] and not enriched:
            updates["photo_ref"] = triage["photo_ref"]
            applied["photo"] = triage_states["photo"]
        if applied:
            states = merge_field_states(states, applied, full=False)
            updates["source_raw_id"] = source_raw_id  # lineage follows the applied columns
    elif newer or (existing["is_min"] and not incoming_min):
        # full<-full and min<-min on recency; min<-full always (richness).
        updates.update(triage)
        if enriched:
            # The avatar is owned by the enrichment lattice from here on.
            updates.pop("photo_ref")
            triage_states = {k: v for k, v in triage_states.items() if k != "photo"}
        bot["user"] = triage_bot
        states = merge_field_states(states, triage_states, full=False)
        updates["is_min"] = int(incoming_min)
        updates["tier"] = tier
        updates["source_raw_id"] = source_raw_id

    # (ii) The enrichment lattice — benchmark `enriched_at`.
    if full_cols is not None and (not enriched or observed_at >= existing["enriched_at"]):
        updates.update(full_cols)
        bot["full"] = full_bot
        states = merge_field_states(states, full_states, full=True)
        updates["enriched_at"] = observed_at
        updates["source_raw_id"] = source_raw_id

    updates["bot_json"] = _bot_json(bot)
    updates["field_states_json"] = dumps(states)
    updates["first_seen"] = min(existing["first_seen"], observed_at)
    updates["last_seen"] = max(existing["last_seen"], observed_at)
    assignments = ", ".join(f"{column} = ?" for column in updates)
    store.conn.execute(f"UPDATE users SET {assignments} WHERE uri = ?", (*updates.values(), uri))
    return uri


def add_user_snapshot(
    store: Store,
    uri: str,
    observed_at: str,
    tier: str,
    method: str,
    bundle: dict,
    source_raw_id: int,
) -> bool:
    """Append one observation iff its bundle differs from the latest snapshot
    of the same `(uri, method)` — like `message_revisions`, a no-change
    re-observation is not a new row. Returns whether a row was written."""
    fields_json = dumps(bundle)
    content_hash = hashlib.sha256(fields_json.encode("utf-8")).hexdigest()
    latest = store.conn.execute(
        "SELECT content_hash FROM user_snapshots WHERE uri=? AND method=? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (uri, method),
    ).fetchone()
    if latest is not None and latest["content_hash"] == content_hash:
        return False
    store.conn.execute(
        "INSERT INTO user_snapshots (uri, observed_at, tier, method, content_hash, fields_json, "
        "source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uri, observed_at, tier, method, content_hash, fields_json, source_raw_id),
    )
    return True


def upsert_user_photo(
    store: Store, uri: str, photo: dict, observed_at: str, source_raw_id: int
) -> None:
    store.conn.execute(
        "INSERT INTO user_photos "
        "(uri, photo_id, date, dc_id, has_video, observed_at, source_raw_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uri, photo_id) DO UPDATE SET "
        "observed_at = MAX(user_photos.observed_at, excluded.observed_at), "
        "date = COALESCE(user_photos.date, excluded.date), "
        "source_raw_id = CASE WHEN excluded.observed_at >= user_photos.observed_at "
        "THEN excluded.source_raw_id ELSE user_photos.source_raw_id END",
        (
            uri, photo["id"], iso_or_none(photo.get("date")), photo.get("dc_id"),
            int(bool(photo.get("video_sizes"))), observed_at, source_raw_id,
        ),
    )


def user_photo_sha(store: Store, uri: str, photo_id: int) -> str | None:
    row = store.conn.execute(
        "SELECT sha256 FROM user_photos WHERE uri=? AND photo_id=?", (uri, photo_id)
    ).fetchone()
    return row["sha256"] if row else None


def set_user_photo_sha(store: Store, uri: str, photo_id: int, sha256: str) -> None:
    store.conn.execute(
        "UPDATE user_photos SET sha256=? WHERE uri=? AND photo_id=?", (sha256, uri, photo_id)
    )
