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
cells stay readable.
"""

from __future__ import annotations

import hashlib
import json

from paperboy.ids import iso_or_none, primary_username, user_uri
from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self

FIELD_STATE_KEYS = ("phone", "photo", "status", "about", "birthday", "forwards", "stories")

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
_FULL_ONLY_COLUMNS = ("about", "birthday")


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
        if full_user.get("profile_photo"):
            states["photo"] = {"state": "present"}
        elif full_user.get("fallback_photo"):
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
    """Newest observation wins per key it can observe — except that a
    triage-level `absent` (no disambiguating capability) never overwrites a
    stored `hidden_from_you` proof; only a newer FULL observation revises it."""
    merged = dict(existing)
    for key, state in incoming.items():
        if (
            not full
            and state.get("state") == "absent"
            and merged.get(key, {}).get("state") == "hidden_from_you"
        ):
            continue
        merged[key] = state
    return merged


def _triage_columns(user: dict) -> dict:
    status = user.get("status") or {}
    status_kind = _STATUS_KINDS.get(_kind(status))
    if status_kind == "online":
        status_value = iso_or_none(status.get("expires"))
    elif status_kind == "offline":
        status_value = iso_or_none(status.get("was_online"))
    else:
        status_value = None

    photo = user.get("photo") or {}
    photo_ref = None
    if _kind(photo) == "userprofilephoto" and not photo.get("personal"):
        photo_ref = dumps(
            {k: photo.get(k) for k in ("photo_id", "dc_id", "has_video", "stripped_thumb")}
        )

    emoji = user.get("emoji_status") or {}
    emoji_status = dumps(emoji) if emoji and _kind(emoji) != "emojistatusempty" else None
    color = {k: user[k] for k in ("color", "profile_color") if user.get(k)}
    usernames = [
        {k: e.get(k) for k in ("username", "editable", "active")}
        for e in (user.get("usernames") or [])
    ]
    bot = {}
    if user.get("bot"):
        bot = {
            k: v for k, v in user.items()
            if k.startswith("bot_") and v is not None and v is not False and v != "" and v != []
        }
    flags = {k: user[k] for k in _TARGET_FLAG_KEYS if user.get(k)}
    restriction = user.get("restriction_reason") or None
    return {
        "id": user["id"],
        "access_hash": user.get("access_hash"),
        "username": primary_username(user),
        "usernames_json": dumps(usernames) if usernames else None,
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "phone": user.get("phone") or None,
        "emoji_status": emoji_status,
        "color_json": dumps(color) if color else None,
        "status_kind": status_kind,
        "status_value": status_value,
        "photo_ref": photo_ref,
        "restriction_json": dumps(restriction) if restriction else None,
        "bot_json": dumps(bot) if bot else None,
        "flags_json": dumps(flags) if flags else None,
    }


def _full_columns(user: dict, full_user: dict, bot_json: str | None) -> dict:
    birthday = full_user.get("birthday") or None
    cols = {
        "about": full_user.get("about") or None,
        "birthday": (
            dumps({k: birthday.get(k) for k in ("day", "month", "year")}) if birthday else None
        ),
    }
    if user.get("bot"):
        # `UserFull` carries the rest of the bot-only surface (bot_info,
        # bot_group_admin_rights, bot_verification, bot_manager_id, ...).
        bot = json.loads(bot_json) if bot_json else {}
        bot.update({
            k: v for k, v in full_user.items()
            if k.startswith("bot_") and v is not None and v is not False and v != "" and v != []
        })
        cols["bot_json"] = dumps(bot) if bot else None
    return cols


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
    collecting account (never a subject, #12). `enriched_at` moves only when
    `full_user` is given, so a triage pass never looks like an enrichment."""
    kind = _kind(user)
    if not kind.startswith("user") or kind == "userempty":
        raise ValueError(f"not a User object: {user.get('_')!r}")
    uri = user_uri(user["id"])
    if is_self(store, uri):
        return None
    incoming_min = bool(user.get("min"))
    cols = _triage_columns(user)
    if full_user is not None:
        cols.update(_full_columns(user, full_user, cols["bot_json"]))
    states = field_states(user, full_user)

    existing = store.conn.execute("SELECT * FROM users WHERE uri=?", (uri,)).fetchone()
    if existing is None:
        cols.update({
            "uri": uri, "tier": tier, "is_min": int(incoming_min),
            "field_states_json": dumps(states),
            "enriched_at": observed_at if full_user is not None else None,
            "source_raw_id": source_raw_id, "first_seen": observed_at, "last_seen": observed_at,
        })
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        store.conn.execute(f"INSERT INTO users ({names}) VALUES ({marks})", tuple(cols.values()))
        return uri

    newer = observed_at >= existing["last_seen"]
    updates: dict = {}
    took_min_branch = incoming_min and not existing["is_min"]
    if took_min_branch:
        # research §8.7: a min object never clobbers a full row's identity.
        # Status applies only if the cached status is empty; photo only with
        # `apply_min_photo`. Both still gated on recency.
        applied: set[str] = set()
        if newer and existing["status_kind"] in (None, "empty") and cols["status_kind"]:
            updates["status_kind"] = cols["status_kind"]
            updates["status_value"] = cols["status_value"]
            applied.add("status")
        if newer and user.get("apply_min_photo") and cols["photo_ref"]:
            updates["photo_ref"] = cols["photo_ref"]
            applied.add("photo")
        if applied:
            # D2: `field_states` must never contradict the columns it
            # describes — a column this branch just wrote to `present`/
            # `hidden_from_you` can't be left recorded `absent`. Merge in
            # ONLY the keys this branch actually applied; every other key
            # (phone, about, ...) is untouched by a min observation and
            # must keep whatever the stored row already said.
            merged_states = merge_field_states(
                json.loads(existing["field_states_json"] or "{}"),
                {key: states[key] for key in applied},
                full=False,
            )
            updates["field_states_json"] = dumps(merged_states)
    elif newer or (existing["is_min"] and not incoming_min):
        # full<-full and min<-min on recency; min<-full always (richness).
        updates.update(cols)
        if full_user is None:
            for column in _FULL_ONLY_COLUMNS:
                updates.pop(column, None)  # triage never blanks full-only columns
            # `bot_json` is written by BOTH levels (`_triage_columns` folds in
            # the bare-`User` bot_* flags; `_full_columns` adds the UserFull-
            # only surface on top) so it isn't a full-only column above — but
            # a triage-level `bot_json` must still never replace a richer one
            # built from a `UserFull`: merge onto the stored bot facts.
            if existing["bot_json"]:
                merged_bot = json.loads(existing["bot_json"])
                incoming_bot = json.loads(updates["bot_json"]) if updates.get("bot_json") else {}
                merged_bot.update(incoming_bot)
                updates["bot_json"] = dumps(merged_bot)
        updates["is_min"] = int(incoming_min)
        updates["tier"] = tier
        updates["source_raw_id"] = source_raw_id
        merged = merge_field_states(
            json.loads(existing["field_states_json"] or "{}"), states, full=full_user is not None
        )
        updates["field_states_json"] = dumps(merged)
    if full_user is not None and not took_min_branch:
        # `enriched_at` must move only when the full-level columns (about,
        # birthday, ...) were actually applied — i.e. the branch above did
        # `updates.update(cols)`. The min branch never applies them (it only
        # ever writes status_kind/status_value/photo_ref), so a row must not
        # be stamped "enriched" while carrying none of the enrichment
        # (a `getFullUser` response is not documented to return a `min`
        # `User`, so this guard is defensive rather than reachable today —
        # but D3's `enriched_at IS NULL` first pass depends on it holding).
        updates["enriched_at"] = max(existing["enriched_at"] or "", observed_at)
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
