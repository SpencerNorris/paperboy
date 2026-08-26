import itertools
import json

import pytest

from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer


def test_min_does_not_clobber_full(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        full = {"_": "user", "id": 9, "access_hash": 111, "username": "real", "first_name": "Real"}
        r1 = st.add_raw("user", full, "member", None)
        upsert_peer(st, full, r1, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        mn = {"_": "user", "id": 9, "min": True, "first_name": "MinName"}
        r2 = st.add_raw("user", mn, "stranger", None)
        upsert_peer(st, mn, r2, "2026-01-02T00:00:00+00:00", seen_in_chat=7, seen_in_msg=34)
        row = st.conn.execute(
            "select username, first_name, is_min, seen_in_msg from peers where uri='tg:user:9'"
        ).fetchone()
        assert row["username"] == "real"
        assert row["first_name"] == "Real"  # not clobbered
        assert row["seen_in_msg"] == 34  # provenance updated
        assert row["is_min"] == 0  # the full row's is_min flag is untouched


def test_first_observation_min_is_stored_as_is(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        mn = {"_": "user", "id": 42, "min": True, "first_name": "OnlyMin"}
        r = st.add_raw("user", mn, "stranger", None)
        uri = upsert_peer(st, mn, r, "2026-01-01T00:00:00+00:00", seen_in_chat=7, seen_in_msg=1)
        assert uri == "tg:user:42"
        row = st.conn.execute("select is_min, first_name from peers where uri=?", (uri,)).fetchone()
        assert row["is_min"] == 1
        assert row["first_name"] == "OnlyMin"


def test_full_observation_overwrites_a_prior_full_observation(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        old = {"_": "user", "id": 9, "username": "old_handle", "first_name": "Old"}
        r1 = st.add_raw("user", old, "member", None)
        upsert_peer(st, old, r1, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        new = {"_": "user", "id": 9, "username": "new_handle", "first_name": "New"}
        r2 = st.add_raw("user", new, "member", None)
        upsert_peer(st, new, r2, "2026-01-02T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        row = st.conn.execute(
            "select username, first_name from peers where uri='tg:user:9'"
        ).fetchone()
        assert row["username"] == "new_handle"
        assert row["first_name"] == "New"


def test_channel_typed_peer_stores_title(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "access_hash": 99, "title": "Durov", "username": "durov"}
        r = st.add_raw("channel", chan, "stranger", None)
        uri = upsert_peer(
            st, chan, r, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None
        )
        assert uri == "tg:channel:5"
        row = st.conn.execute("select kind, title from peers where uri=?", (uri,)).fetchone()
        assert row["kind"] == "channel"
        assert row["title"] == "Durov"


def test_out_of_order_observation_keeps_seen_window_and_newest_state(tmp_path):
    # ADR-0005 §6: replay serves records in RECORDED order, not necessarily
    # observed_at order across runs — an older observation can arrive at
    # upsert_peer AFTER a newer one already landed. first_seen/last_seen must
    # widen to the true window regardless of arrival order, and stale data
    # must never clobber a newer value.
    with Store.open(tmp_path / "p.sqlite") as store:
        raw_new = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        raw_old = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        upsert_peer(store, {"_": "user", "id": 9, "username": "new_name"},
                    raw_new, "2026-02-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        # The OLDER observation arrives second (out of order):
        upsert_peer(store, {"_": "user", "id": 9, "username": "old_name"},
                    raw_old, "2026-01-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        row = store.conn.execute(
            "SELECT username, first_seen, last_seen FROM peers"
        ).fetchone()
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen"] == "2026-02-01T00:00:00+00:00"
        assert row["username"] == "new_name"   # stale data must not clobber


def test_min_observation_older_than_first_seen_still_widens_the_window(tmp_path):
    # adversarial-reviewer, round 2: the min-over-an-existing-non-min-row
    # branch widened only `last_seen` (MAX), never `first_seen` (MIN) — a
    # regression against this module's own documented invariant
    # ("first_seen/last_seen themselves always widen to the true min/max
    # window regardless of arrival order"). A min observation OLDER than the
    # stored full row's first_seen must still pull first_seen backward, the
    # same way the full-upsert branch already does (`test_out_of_order_
    # observation_keeps_seen_window_and_newest_state` above covers that
    # branch; this covers the min branch, which took a different code path
    # untouched by that test).
    with Store.open(tmp_path / "p.sqlite") as st:
        full = {"_": "user", "id": 9, "access_hash": 111, "username": "real"}
        r1 = st.add_raw("user", full, "member", None)
        upsert_peer(st, full, r1, "2026-01-02T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        mn = {"_": "user", "id": 9, "min": True}
        r2 = st.add_raw("user", mn, "stranger", None)
        # The min observation is OLDER than the stored row's first_seen —
        # replay can serve records out of `observed_at` order (ADR-0005 §6).
        upsert_peer(st, mn, r2, "2026-01-01T00:00:00+00:00", seen_in_chat=7, seen_in_msg=1)
        row = st.conn.execute(
            "select first_seen, last_seen, username from peers where uri='tg:user:9'"
        ).fetchone()
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"  # widened backward
        assert row["last_seen"] == "2026-01-02T00:00:00+00:00"  # unaffected
        assert row["username"] == "real"  # stale min observation didn't clobber state


def test_join_to_send_flag_is_persisted(tmp_path):
    """`join_to_send` is the passivity guardrail the `discussion` collector's
    preflight relies on to decide whether reading a linked group requires
    membership (design spec §4.3) — `docs/data-model.md` already documents
    it as a stored flag. `_FLAG_KEYS` currently omits it, so it is silently
    dropped here: `upsert_peer` of a `Channel` carrying `join_to_send: True`
    stores `flags_json` with no trace of it, which is a production gap
    (`_FLAG_KEYS` needs `"join_to_send"` added), not a test-fixture problem.
    Pinning it here means the discussion-collector test that seeds this
    flag through `upsert_peer` cannot be made to pass by writing
    `flags_json` directly in that test's own helper instead of fixing this
    projection — see `tests/test_collector_discussion.py::
    test_skips_when_the_group_requires_joining_to_read`."""
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {
            "_": "Channel", "id": 77, "access_hash": 4242, "title": "C Chat",
            "megagroup": True, "join_to_send": True,
        }
        r = st.add_raw("channel", chan, "stranger", None)
        uri = upsert_peer(
            st, chan, r, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None
        )
        row = st.conn.execute("select flags_json from peers where uri=?", (uri,)).fetchone()
        flags = json.loads(row["flags_json"]) if row["flags_json"] else {}
        assert flags.get("join_to_send") is True


# --- ADR-0005 §6 (amended, round-2 finding A): the composed richness ∘
# recency lattice, table-tested cell by cell. --------------------------------

_T_STORED = "2026-01-05T00:00:00+00:00"
_T_OLDER = "2026-01-01T00:00:00+00:00"   # < _T_STORED
_T_NEWER = "2026-01-10T00:00:00+00:00"   # > _T_STORED

_STORED_FULL = {"_": "user", "id": 9, "access_hash": 111, "username": "stored_full",
                "first_name": "StoredFull"}
_STORED_MIN = {"_": "user", "id": 9, "min": True, "first_name": "StoredMin"}
_INCOMING_FULL = {"_": "user", "id": 9, "access_hash": 222, "username": "incoming_full",
                   "first_name": "IncomingFull"}
_INCOMING_MIN = {"_": "user", "id": 9, "min": True, "first_name": "IncomingMin"}

_STORED_PROV = (100, 200)      # seen_in_chat, seen_in_msg for the stored observation
_INCOMING_PROV = (300, 400)    # seen_in_chat, seen_in_msg for the incoming observation


def _seed(store, stored_obj, stored_prov):
    r = store.add_raw("user", stored_obj, "member", None, observed_at=_T_STORED)
    upsert_peer(store, stored_obj, r, _T_STORED,
                seen_in_chat=stored_prov[0], seen_in_msg=stored_prov[1])
    return r


# Each case: (stored_kind, incoming_kind, incoming_stamp,
#             identity_from, provenance_from, source_raw_from,
#             expected_first_seen, expected_last_seen)
# "identity_from"/"provenance_from"/"source_raw_from" are "stored" or
# "incoming" -- which observation's values must appear in the final row.
_LATTICE_CASES = [
    # full <- full: recency for everything.
    ("full", "full", "older", "stored", "stored", "stored", _T_OLDER, _T_STORED),
    ("full", "full", "newer", "incoming", "incoming", "incoming", _T_STORED, _T_NEWER),
    # full <- min: richness -- identity NEVER moves; provenance (+source_raw_id) on recency.
    ("full", "min", "older", "stored", "stored", "stored", _T_OLDER, _T_STORED),
    ("full", "min", "newer", "stored", "incoming", "incoming", _T_STORED, _T_NEWER),
    # min <- full: richness -- identity (+source_raw_id) ALWAYS moves; provenance on recency.
    ("min", "full", "older", "incoming", "stored", "incoming", _T_OLDER, _T_STORED),
    ("min", "full", "newer", "incoming", "incoming", "incoming", _T_STORED, _T_NEWER),
    # min <- min: recency for everything.
    ("min", "min", "older", "stored", "stored", "stored", _T_OLDER, _T_STORED),
    ("min", "min", "newer", "incoming", "incoming", "incoming", _T_STORED, _T_NEWER),
]


@pytest.mark.parametrize(
    "stored_kind, incoming_kind, incoming_stamp, identity_from, provenance_from, "
    "source_raw_from, expected_first_seen, expected_last_seen",
    _LATTICE_CASES,
    ids=[f"{c[0]}_stored-{c[1]}_incoming-{c[2]}" for c in _LATTICE_CASES],
)
def test_upsert_peer_composed_lattice(
    tmp_path, stored_kind, incoming_kind, incoming_stamp, identity_from,
    provenance_from, source_raw_from, expected_first_seen, expected_last_seen,
):
    """ADR-0005 §6 (amended): the eight (stored richness x incoming richness x
    incoming recency) cells of the composed lattice. Round 2 shipped a
    recency-only rule that silently dropped `peers.py`'s pre-existing richness
    invariant; two cells broke as a result -- both are covered here
    (`min_stored-full_incoming-older` is the one the round-2 escalation
    named explicitly: a full incoming observation with an OLDER stamp than a
    stored `min` row must still win, because a `min` row never had
    trustworthy profile data to begin with)."""
    stored_obj = _STORED_FULL if stored_kind == "full" else _STORED_MIN
    incoming_obj = _INCOMING_FULL if incoming_kind == "full" else _INCOMING_MIN
    incoming_t = _T_OLDER if incoming_stamp == "older" else _T_NEWER

    with Store.open(tmp_path / "p.sqlite") as store:
        stored_raw = _seed(store, stored_obj, _STORED_PROV)
        incoming_raw = store.add_raw("user", incoming_obj, "stranger", None,
                                      observed_at=incoming_t)
        upsert_peer(store, incoming_obj, incoming_raw, incoming_t,
                    seen_in_chat=_INCOMING_PROV[0], seen_in_msg=_INCOMING_PROV[1])

        row = store.conn.execute(
            "SELECT username, is_min, seen_in_chat, seen_in_msg, source_raw_id, "
            "first_seen, last_seen FROM peers WHERE uri='tg:user:9'"
        ).fetchone()

    want_identity = stored_obj if identity_from == "stored" else incoming_obj
    assert row["username"] == want_identity.get("username")
    assert row["is_min"] == int(bool(want_identity.get("min")))

    want_prov = _STORED_PROV if provenance_from == "stored" else _INCOMING_PROV
    assert (row["seen_in_chat"], row["seen_in_msg"]) == want_prov

    want_raw = stored_raw if source_raw_from == "stored" else incoming_raw
    assert row["source_raw_id"] == want_raw

    assert row["first_seen"] == expected_first_seen
    assert row["last_seen"] == expected_last_seen


def test_upsert_peer_lattice_is_order_independent_for_a_replay_sequence(tmp_path):
    """The ordering-property gate the #33 round-2 escalation demanded: a
    class of test that can fail on a future ordering defect without a
    reviewer having to invent a fresh probe for it (rather than one more
    fixed-order example, which round 2's own gap showed is easy to write
    around by accident).

    Three observations of the same peer -- full@t1 (oldest), min@t2, full@t3
    (newest) -- applied in every one of the six possible arrival orders must
    converge on the identical final row: replay does not guarantee
    observed_at-order delivery within upsert_peer (ADR-0005 §6's own
    `test_out_of_order_observation_keeps_seen_window_and_newest_state` above
    already pins that for a single full<-full pair), so the merge must be
    genuinely order-independent, not just correct for the one arrival order a
    live collect happens to produce.

    Scope note: this triple is chosen so `min`'s own stamp never exceeds
    BOTH full stamps. When it does (a `min` observation strictly newer than
    every `full` observation of the same peer, arriving between two `full`
    observations in processing order), the composed lattice as specified by
    ADR-0005 §6 is provably NOT order-independent -- richness correctly
    promotes whichever `full` observation is current at the moment the
    later, "poisoned" `last_seen` benchmark was set, and *which* `full` that
    is depends on arrival order, not just timestamps (`peers.last_seen` is
    one shared column standing in for two different benchmarks: "freshest
    observation of any kind" and "freshest full/identity observation").
    Filed as #38 (real-world reachable via replay's payload-keyed,
    non-chronological delivery -- see `ReplayClock`'s docstring) rather than
    fixed here: round 3 was authorized narrow, and closing it needs a real
    design change (a richness-scoped timestamp benchmark distinct from
    `last_seen`), not a guard tweak.
    """
    t1 = "2026-01-01T00:00:00+00:00"
    t2 = "2026-01-02T00:00:00+00:00"
    t3 = "2026-01-03T00:00:00+00:00"
    observations = {
        "full1": ({"_": "user", "id": 9, "access_hash": 1, "username": "u1"}, t1, (10, 20)),
        "min2": ({"_": "user", "id": 9, "min": True}, t2, (30, 40)),
        "full3": ({"_": "user", "id": 9, "access_hash": 3, "username": "u3"}, t3, (50, 60)),
    }
    results = []
    for order in itertools.permutations(observations):
        with Store.open(tmp_path / f"p_{'_'.join(order)}.sqlite") as store:
            for label in order:
                obj, t, (sc, sm) = observations[label]
                raw_id = store.add_raw("user", obj, "stranger", None, observed_at=t)
                upsert_peer(store, obj, raw_id, t, seen_in_chat=sc, seen_in_msg=sm)
            row = store.conn.execute(
                "SELECT username, access_hash, is_min, seen_in_chat, seen_in_msg, "
                "first_seen, last_seen FROM peers"
            ).fetchone()
            results.append(dict(row))

    assert all(r == results[0] for r in results), results
    # Matches the lattice's expected outcome: the newest AND full observation
    # wins outright, with its own provenance and the fully-widened window.
    assert results[0] == {
        "username": "u3", "access_hash": 3, "is_min": 0,
        "seen_in_chat": 50, "seen_in_msg": 60,
        "first_seen": t1, "last_seen": t3,
    }
