"""SQLite storage for orders and tracked packages.

Schema evolution: v1 used `tracking_number` as the primary key. v2 adds
pre-shipment rows that have no tracking number yet, so the PK becomes a
synthetic `id` and `tracking_number` is a nullable unique column. A migration
in `init_schema()` rebuilds the table for existing v1 databases.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_number        TEXT UNIQUE,
    order_number           TEXT,
    description            TEXT,
    vendor                 TEXT,
    po_number              TEXT,
    carrier                TEXT,
    easypost_id            TEXT,
    status                 TEXT,
    ordered_date           TEXT,
    promised_ship_date     TEXT,
    promised_delivery_date TEXT,
    est_delivery           TEXT,
    last_event             TEXT,
    last_event_time        TEXT,
    last_event_location    TEXT,
    events_json            TEXT,
    tracker_error          TEXT,
    tracker_error_at       TEXT,
    tracking_url           TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_status ON packages(status);
CREATE INDEX IF NOT EXISTS idx_packages_order_number ON packages(order_number);
CREATE INDEX IF NOT EXISTS idx_packages_po_number ON packages(po_number);
"""

TERMINAL_STATUSES = {"delivered", "cancelled", "return_to_sender", "failure", "error"}


def default_db_path() -> Path:
    override = os.environ.get("MAILROOM_DB") or os.environ.get("PROCUREMENT_TRACKER_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Mailroom" / ".mailroom" / "db.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Rebuild the v1 `packages` table (tracking_number PK) into v2 (id PK)."""
    info = conn.execute("PRAGMA table_info(packages)").fetchall()
    if not info:
        return  # table doesn't exist yet, nothing to migrate
    cols = {row[1]: row for row in info}
    if "id" in cols and "order_number" in cols:
        return  # already v2

    conn.executescript(
        """
        ALTER TABLE packages RENAME TO packages_v1_backup;

        CREATE TABLE packages (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_number        TEXT UNIQUE,
            order_number           TEXT,
            description            TEXT,
            vendor                 TEXT,
            po_number              TEXT,
            carrier                TEXT,
            easypost_id            TEXT,
            status                 TEXT,
            ordered_date           TEXT,
            promised_ship_date     TEXT,
            promised_delivery_date TEXT,
            est_delivery           TEXT,
            last_event             TEXT,
            last_event_time        TEXT,
            last_event_location    TEXT,
            events_json            TEXT,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL
        );

        INSERT INTO packages (
            tracking_number, description, vendor, po_number, carrier,
            easypost_id, status, est_delivery,
            last_event, last_event_time, last_event_location, events_json,
            created_at, updated_at
        )
        SELECT
            tracking_number, description, vendor, po_number, carrier,
            easypost_id, status, est_delivery,
            last_event, last_event_time, last_event_location, events_json,
            created_at, updated_at
        FROM packages_v1_backup;

        DROP TABLE packages_v1_backup;
        """
    )


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after v2 to existing databases (idempotent)."""
    info = conn.execute("PRAGMA table_info(packages)").fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for col, ddl in [
        ("tracker_error", "TEXT"),
        ("tracker_error_at", "TEXT"),
        ("tracking_url", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE packages ADD COLUMN {col} {ddl}")


def init_schema(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        _migrate_v1_to_v2(conn)
        conn.executescript(SCHEMA)
        _migrate_add_columns(conn)


def add_package(
    tracking_number: str | None = None,
    description: str | None = None,
    vendor: str | None = None,
    po_number: str | None = None,
    order_number: str | None = None,
    carrier: str | None = None,
    easypost_id: str | None = None,
    status: str | None = None,
    ordered_date: str | None = None,
    promised_ship_date: str | None = None,
    promised_delivery_date: str | None = None,
    tracking_url: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert a new row and return its id."""
    now = _now()
    default_status = status or ("pre_transit" if tracking_number else "ordered")
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO packages (
                tracking_number, order_number, description, vendor, po_number,
                carrier, easypost_id, status,
                ordered_date, promised_ship_date, promised_delivery_date,
                tracking_url,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracking_number,
                order_number,
                description,
                vendor,
                po_number,
                carrier,
                easypost_id,
                default_status,
                ordered_date,
                promised_ship_date,
                promised_delivery_date,
                tracking_url,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def update_package(
    row_id: int,
    *,
    tracking_number: str | None = None,
    order_number: str | None = None,
    description: str | None = None,
    vendor: str | None = None,
    po_number: str | None = None,
    carrier: str | None = None,
    easypost_id: str | None = None,
    status: str | None = None,
    ordered_date: str | None = None,
    promised_ship_date: str | None = None,
    promised_delivery_date: str | None = None,
    tracking_url: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Patch any subset of fields on an existing row. None means "leave alone"."""
    fields = {
        "tracking_number": tracking_number,
        "order_number": order_number,
        "description": description,
        "vendor": vendor,
        "po_number": po_number,
        "carrier": carrier,
        "easypost_id": easypost_id,
        "status": status,
        "ordered_date": ordered_date,
        "promised_ship_date": promised_ship_date,
        "promised_delivery_date": promised_delivery_date,
        "tracking_url": tracking_url,
    }
    set_parts = [f"{k} = COALESCE(?, {k})" for k in fields]
    params = list(fields.values()) + [_now(), row_id]
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE packages SET {', '.join(set_parts)}, updated_at = ? WHERE id = ?",
            params,
        )


def update_status(
    row_id: int,
    status: str,
    est_delivery: str | None,
    last_event: str | None,
    last_event_time: str | None,
    last_event_location: str | None,
    events: list[dict[str, Any]] | None,
    carrier: str | None = None,
    easypost_id: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Write carrier-polled status back to a row."""
    events_json = json.dumps(events) if events is not None else None
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE packages SET
                status              = ?,
                est_delivery        = ?,
                last_event          = ?,
                last_event_time     = ?,
                last_event_location = ?,
                events_json         = COALESCE(?, events_json),
                carrier             = COALESCE(?, carrier),
                easypost_id         = COALESCE(?, easypost_id),
                tracker_error       = NULL,
                tracker_error_at    = NULL,
                updated_at          = ?
            WHERE id = ?
            """,
            (
                status,
                est_delivery,
                last_event,
                last_event_time,
                last_event_location,
                events_json,
                carrier,
                easypost_id,
                _now(),
                row_id,
            ),
        )


def set_tracker_error(
    row_id: int, error: str | None, db_path: Path | None = None
) -> None:
    """Record (or clear) the last EasyPost failure for a row."""
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE packages SET tracker_error = ?, tracker_error_at = ?, updated_at = ? WHERE id = ?",
            (error, now if error else None, now, row_id),
        )


def find_match(
    tracking_number: str | None = None,
    order_number: str | None = None,
    po_number: str | None = None,
    vendor: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Look up an existing row using the best available identifier.

    Priority: tracking_number (exact) → order_number (+ vendor if given) → po_number.
    Returns the row as a dict, or None.
    """
    with connect(db_path) as conn:
        if tracking_number:
            row = conn.execute(
                "SELECT * FROM packages WHERE tracking_number = ?", (tracking_number,)
            ).fetchone()
            if row:
                return dict(row)
        if order_number:
            if vendor:
                row = conn.execute(
                    "SELECT * FROM packages WHERE order_number = ? AND vendor = ? LIMIT 1",
                    (order_number, vendor),
                ).fetchone()
                if row:
                    return dict(row)
            row = conn.execute(
                "SELECT * FROM packages WHERE order_number = ? LIMIT 1",
                (order_number,),
            ).fetchone()
            if row:
                return dict(row)
        if po_number:
            row = conn.execute(
                "SELECT * FROM packages WHERE po_number = ? LIMIT 1", (po_number,)
            ).fetchone()
            if row:
                return dict(row)
    return None


def get_package(row_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM packages WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else None


# Sort expressions. Date-like columns fall back across related columns so pre-shipment rows
# (no est_delivery / no last_event_time yet) still sort sensibly.
SORT_EXPRESSIONS = {
    "status": "status",
    "description": "description COLLATE NOCASE",
    "vendor": "vendor COLLATE NOCASE",
    "carrier": "carrier COLLATE NOCASE",
    "order_number": "order_number COLLATE NOCASE",
    "tracking_number": "tracking_number COLLATE NOCASE",
    "est_delivery": "COALESCE(est_delivery, promised_delivery_date)",
    "last_event_time": "COALESCE(last_event_time, ordered_date, created_at)",
    "promised_delivery_date": "promised_delivery_date",
    "ordered_date": "ordered_date",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
SORTABLE_COLUMNS = set(SORT_EXPRESSIONS)


def list_packages(
    include_delivered: bool = False,
    sort_by: str = "last_event_time",
    sort_dir: str = "desc",
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    expr = SORT_EXPRESSIONS.get(sort_by, SORT_EXPRESSIONS["last_event_time"])
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    if include_delivered:
        query = "SELECT * FROM packages"
        params: tuple[Any, ...] = ()
    else:
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        query = (
            f"SELECT * FROM packages WHERE status IS NULL OR status NOT IN ({placeholders})"
        )
        params = tuple(TERMINAL_STATUSES)
    # `(expr) IS NULL` emulates NULLS LAST: nulls sort to the bottom regardless of direction.
    query += f" ORDER BY ({expr}) IS NULL, {expr} {direction}, created_at DESC"
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_package(row_id: int, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM packages WHERE id = ?", (row_id,))


def status_counts(db_path: Path | None = None) -> dict[str, int]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM packages GROUP BY status"
        ).fetchall()
    return {row["status"] or "unknown": row["n"] for row in rows}


PRE_SHIPMENT_STATUSES = {"ordered", "confirmed", "in_fulfillment"}


def pre_shipment_count(db_path: Path | None = None) -> int:
    counts = status_counts(db_path)
    return sum(counts.get(s, 0) for s in PRE_SHIPMENT_STATUSES)
