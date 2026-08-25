"""`run_events`: the durable audit of what a collect actually did.

One row per phase (complete / skip / phase_stop / hard_stop), plus any
non-passive act — notably a `--join` (issue #20), which is the one write
paperboy makes and so is recorded here as an explicit, auditable event.
"""

from __future__ import annotations

from paperboy.ids import utc_now_iso
from paperboy.store.db import Store, dumps


def record_run_event(
    store: Store,
    channel_id: int | None,
    phase: str,
    kind: str,
    detail: dict | None,
) -> None:
    store.conn.execute(
        "INSERT INTO run_events(observed_at, channel_id, phase, kind, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (utc_now_iso(), channel_id, phase, kind, dumps(detail) if detail is not None else None),
    )
