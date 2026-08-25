"""The graph projection: triple-shaped `edges` (ADR-0002).

Every entity has a URI id, so `(subject, predicate, object)` plus provenance
(`observed_at`, `tier`, `source_raw_id`, `evidence_json`) is enough for
RDF/Turtle or GraphML to be an export projection rather than a second store.
"""

from __future__ import annotations

from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self


def add_edge(
    store: Store,
    subject: str,
    predicate: str,
    object_: str,
    observed_at: str,
    tier: str,
    source_raw_id: int | None,
    evidence: dict | None,
) -> bool:
    """Insert one edge, returning whether it was written.

    An edge whose subject or object is the collecting account is skipped and
    returns `False` — the projection-layer chokepoint that keeps the collector
    out of the graph regardless of which producer built the edge (issue #12).
    Callers that count edges must increment only on a `True` return.
    """
    if is_self(store, subject) or is_self(store, object_):
        return False
    store.conn.execute(
        "INSERT INTO edges (subject_uri, predicate, object_uri, observed_at, tier, "
        "source_raw_id, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            subject, predicate, object_, observed_at, tier, source_raw_id,
            dumps(evidence) if evidence is not None else None,
        ),
    )
    return True


def edge_exists(store: Store, subject: str, predicate: str, object_: str) -> bool:
    """Whether the exact `(subject, predicate, object)` triple is already stored."""
    return (
        store.conn.execute(
            "SELECT 1 FROM edges WHERE subject_uri=? AND predicate=? AND object_uri=? LIMIT 1",
            (subject, predicate, object_),
        ).fetchone()
        is not None
    )


def add_edge_once(
    store: Store,
    subject: str,
    predicate: str,
    object_: str,
    observed_at: str,
    tier: str,
    source_raw_id: int | None,
    evidence: dict | None,
) -> bool:
    """`add_edge`, skipped when the identical triple is already stored.

    For *structural* facts — "X mentions Y", "X recommended-with Y", "X commented
    on Y", "X member-of Y" — which are set-like, not time-varying observations
    like `message_metrics`. A collector that re-scans stored rows every run (or a
    re-run after a page-budget stop) would otherwise append a duplicate row with
    a fresh `observed_at` and the previous run's `source_raw_id` as evidence this
    run never gathered (issues #14, #19). `add_edge` itself stays a bare INSERT
    so `channel`/`history` keep their append-only semantics (ADR-0002).
    """
    if edge_exists(store, subject, predicate, object_):
        return False
    return add_edge(store, subject, predicate, object_, observed_at, tier, source_raw_id, evidence)
