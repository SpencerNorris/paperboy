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

    def add_raw(self, kind: str, payload: dict, tier: str, context: dict | None) -> int:
        """Append one TL object (as `to_dict()`) to the raw log; returns its rowid."""
        cur = self.conn.execute(
            "INSERT INTO raw_records(kind, observed_at, tier, context_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                kind,
                utc_now_iso(),
                tier,
                dumps(context) if context is not None else None,
                dumps(payload),
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
