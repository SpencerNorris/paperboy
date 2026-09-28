"""SQLite connection lifecycle, migrations, and the raw-record log.

SQLite is the system of record (ADR-0002): WAL mode, foreign keys on,
explicit `migrations/NNNN_*.sql` files tracked in `schema_migrations`. Every
TL object observed is appended to `raw_records` (see `add_raw`) *before* any
projection — projections carry `source_raw_id` and can be rebuilt from raw
after a Telethon layer bump.

Connections use `isolation_level=None` (autocommit): every statement is
durable the instant it runs, so a killed process never loses a write that
already executed. This trades cross-table atomicity (e.g. a message update
and its revision row are two separate commits) for the simpler, and for this
tool more important, guarantee that nothing is silently rolled back on
close — every writer here is idempotent (`INSERT ... ON CONFLICT DO UPDATE`,
`CREATE ... IF NOT EXISTS`), so a re-run after an interruption repairs state
rather than needing a transaction to undo.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from paperboy.ids import to_iso, utc_now_iso

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _json_default(value: object) -> object:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def dumps(payload: object) -> str:
    """Canonical JSON used for every json-text column: sorted keys, UTF-8 text."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)


class Store:
    """Owns one SQLite connection: pragmas, migrations, and the raw log.

    Prefer `Store.open(path)` (a context manager) over constructing directly.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._run_id: str | None = None

    @classmethod
    def open(cls, path: Path) -> Self:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn)
        store._apply_migrations()
        return store

    def _apply_migrations(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row["name"] for row in self.conn.execute("SELECT name FROM schema_migrations")}
        for sql_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            stem = sql_path.stem
            if stem in applied:
                continue
            self.conn.executescript(sql_path.read_text())
            self.conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (stem, utc_now_iso()),
            )

    def begin_run(self, run_id: str | None = None) -> str:
        """Mark the start of one collect pass (ADR-0005): every subsequent
        `add_raw` carries this id, so replay can reconstruct pass boundaries.
        Replay injects the SOURCE run's id, making a reprojected DB itself
        re-reprojectable; live runs take a fresh opaque id."""
        self._run_id = run_id if run_id is not None else uuid4().hex
        return self._run_id

    @property
    def run_id(self) -> str | None:
        """The current collect pass's id (`begin_run`), or `None` before one
        begins — lets a collector do something exactly once per run."""
        return self._run_id

    def add_raw(
        self,
        kind: str,
        payload: dict,
        tier: str,
        context: dict | None,
        observed_at: str | None = None,
    ) -> int:
        """Append one TL object (as `to_dict()`) to the raw log; returns its rowid.

        `observed_at` is the caller's per-record observation stamp — the same
        value the caller passes to every projection of this record, so raw and
        projection agree (spec §5, the reproject clock seam). `None` (legacy
        callers, tests) stamps now. `run_id` (ADR-0005) is whatever
        `begin_run` last set — `None` until a run is begun, which is how
        legacy pre-migration rows and any caller that skips `begin_run` stay
        NULL, distinguishable from a stamped run at replay time.
        """
        cur = self.conn.execute(
            "INSERT INTO raw_records(kind, observed_at, tier, context_json, payload_json, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                observed_at if observed_at is not None else utc_now_iso(),
                tier,
                dumps(context) if context is not None else None,
                dumps(payload),
                self._run_id,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
