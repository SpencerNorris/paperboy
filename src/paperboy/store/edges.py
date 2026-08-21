"""The graph projection: triple-shaped `edges` (ADR-0002).

Every entity has a URI id, so `(subject, predicate, object)` plus provenance
(`observed_at`, `tier`, `source_raw_id`, `evidence_json`) is enough for
RDF/Turtle or GraphML to be an export projection rather than a second store.
"""

from __future__ import annotations

from paperboy.store.db import Store, dumps


def add_edge(
    store: Store,
    subject: str,
    predicate: str,
    object_: str,
    observed_at: str,
    tier: str,
    source_raw_id: int | None,
    evidence: dict | None,
) -> None:
    store.conn.execute(
        "INSERT INTO edges (subject_uri, predicate, object_uri, observed_at, tier, "
        "source_raw_id, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            subject, predicate, object_, observed_at, tier, source_raw_id,
            dumps(evidence) if evidence is not None else None,
        ),
    )
