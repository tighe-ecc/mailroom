"""SQLite storage for orders and tracked packages.

Schema evolution: v1 used `tracking_number` as the primary key. v2 adds
pre-shipment rows that have no tracking number yet, so the PK becomes a
synthetic `id` and `tracking_number` is a nullable unique column. A migration
in `init_schema()` rebuilds the table for existing v1 databases.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_number        TEXT UNIQUE,
    order_number           TEXT,
    description            TEXT,
    vendor                 TEXT,
    sender_domain          TEXT,
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
-- sender_domain index is created in _migrate_add_columns so the index DDL
-- doesn't run before the column has been added on existing v2 databases.
"""

TERMINAL_STATUSES = {"delivered", "cancelled", "return_to_sender", "failure", "error"}

# Lifecycle ordering. Used to detect regressions — e.g. a late-arriving
# order-confirmation email should not downgrade an already-shipped row to
# "confirmed". Values are co-equal across "out_for_delivery" and
# "available_for_pickup" because they're parallel branches of the same
# in-flight stage.
STATUS_RANK = {
    "unknown": -1,
    "ordered": 0,
    "confirmed": 1,
    "in_fulfillment": 2,
    "pre_transit": 3,
    "in_transit": 4,
    "out_for_delivery": 5,
    "available_for_pickup": 5,
    "delivered": 6,
    "return_to_sender": 6,
    "failure": 6,
    "cancelled": 6,
    "error": 6,
}


def is_status_regression(old: str | None, new: str | None) -> bool:
    """True iff `new` would move the row backward in the lifecycle vs `old`."""
    if not new or not old or old == new:
        return False
    return STATUS_RANK.get(new, -1) < STATUS_RANK.get(old, -1)


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
        ("sender_domain", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE packages ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_packages_sender_domain ON packages(sender_domain)"
    )


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
    sender_domain: str | None = None,
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
                tracking_url, sender_domain,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                sender_domain,
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
    sender_domain: str | None = None,
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
        "sender_domain": sender_domain,
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


_ID_NOISE = re.compile(r"[\s\-_/.#]+")
_ID_PREFIX = re.compile(
    r"^(ORDER|ORD|INVOICE|INV|PO|REF)[\s\-_/.#]+", re.IGNORECASE
)


def _norm_id(value: str | None) -> str | None:
    """Normalize an order/PO/tracking ID for fuzzy comparison.

    Drops a leading vendor prefix like "ORD" / "PO" / "INV" *if* it was
    followed by a separator in the original (so "Order #12345" matches "12345"
    but "POPULAR-1" stays intact), then strips whitespace and common
    separators (``- _ / . #``) and uppercases. Pure-numeric IDs additionally
    have leading zeros stripped.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = _ID_PREFIX.sub("", s, count=1)
    s = _ID_NOISE.sub("", s).upper()
    if not s:
        return None
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def _norm_vendor(value: str | None) -> str | None:
    if value is None:
        return None
    s = re.sub(r"[\s,.\-]+", "", str(value)).upper()
    for suffix in ("CORPORATION", "CORP", "INCORPORATED", "INC", "LLC", "LTD", "CO"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s or None


# Email subdomains routinely used for transactional mail. Stripping these so
# `mail.mark-10.com` and `notifications.mark-10.com` and `mark-10.com` all
# normalize to `mark-10.com`.
_EMAIL_SUBDOMAINS = (
    "mail", "email", "e", "smtp", "send", "sender", "mailer",
    "notifications", "notification", "notify", "info",
    "orders", "order", "shipping", "ship", "shipment",
    "tracking", "track", "support", "no-reply", "noreply", "donotreply",
    "bounce", "bounces", "reply", "post",
)


def _norm_domain(value: str | None) -> str | None:
    """Normalize a domain for comparison.

    Lowercases, drops `www.`, and strips a leading transactional subdomain
    (`mail.`, `notifications.`, `orders.`, …) so different email gateways
    of the same vendor compare equal.
    """
    if value is None:
        return None
    s = str(value).strip().lower().lstrip(".")
    if not s:
        return None
    if s.startswith("www."):
        s = s[4:]
    parts = s.split(".")
    while len(parts) >= 3 and parts[0] in _EMAIL_SUBDOMAINS:
        parts = parts[1:]
    return ".".join(parts) or None


def extract_sender_domain(sender_header: str | None) -> str | None:
    """Pull the (normalized) domain out of an RFC 5322 From header.

    Handles `"Name" <addr@host>` and bare `addr@host`. Returns the normalized
    domain — see ``_norm_domain`` — or None if no `@` is present.
    """
    if not sender_header:
        return None
    from email.utils import parseaddr

    _, addr = parseaddr(sender_header)
    if "@" not in addr:
        return None
    return _norm_domain(addr.rsplit("@", 1)[1])


VENDOR_FALLBACK_DAYS = 90


def find_match(
    tracking_number: str | None = None,
    order_number: str | None = None,
    po_number: str | None = None,
    vendor: str | None = None,
    sender_domain: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Look up an existing row using the best available identifier.

    Priority:
      1. tracking_number (exact)
      2. order_number — normalized comparison, narrowed by sender_domain
         (preferred) or vendor name when ambiguous
      3. po_number — same
      4. cross-field — order_number matches an existing po_number or vice-versa
      5. sender-domain fallback — exactly one open (no tracking) row from same
         sender domain within the last 90 days. Vendor name is only consulted
         as a last-resort when sender_domain isn't available (manual entries).

    Sender domain is the From-header host (e.g. ``mark-10.com``); it's stable
    across order-confirmation and shipping-confirmation emails from the same
    vendor in a way that LLM-extracted vendor names ("MARK-10 Corp" vs
    "Mark-10 Corporation") aren't.

    Normalized ID comparison strips whitespace/separators, uppercases, drops
    common prefixes like "ORD" / "PO" / "INV", and ignores leading zeros, so
    minor extraction variance ("ORD-12345" vs "12345") still matches.
    """
    norm_order = _norm_id(order_number)
    norm_po = _norm_id(po_number)
    norm_vendor = _norm_vendor(vendor)
    norm_domain = _norm_domain(sender_domain)
    with connect(db_path) as conn:
        if tracking_number:
            row = conn.execute(
                "SELECT * FROM packages WHERE tracking_number = ?", (tracking_number,)
            ).fetchone()
            if row:
                return dict(row)

        if norm_order:
            match = _scan_for_id(conn, "order_number", norm_order, norm_domain, norm_vendor)
            if match:
                return match

        if norm_po:
            match = _scan_for_id(conn, "po_number", norm_po, norm_domain, norm_vendor)
            if match:
                return match

        # Cross-field: vendors don't always agree on which is "order" vs "PO".
        if norm_order:
            match = _scan_for_id(conn, "po_number", norm_order, norm_domain, norm_vendor)
            if match:
                return match
        if norm_po:
            match = _scan_for_id(conn, "order_number", norm_po, norm_domain, norm_vendor)
            if match:
                return match

        # Sender-domain fallback: only when the inbound email has a tracking
        # number (i.e. is a shipping confirmation). Pairs the shipping
        # confirmation with the most recent un-shipped order from the same
        # sender domain. Domain is far more stable than vendor-name strings
        # extracted by the LLM, so we prefer it; we only fall back to
        # vendor-name matching if no domain is available.
        if tracking_number and (norm_domain or norm_vendor):
            match = _open_row_fallback(conn, norm_domain, norm_vendor)
            if match:
                return match
    return None


def _scan_for_id(
    conn: sqlite3.Connection,
    column: str,
    target_norm: str,
    domain_norm: str | None,
    vendor_norm: str | None,
) -> dict[str, Any] | None:
    """Find a row whose <column> normalizes to target_norm.

    When multiple rows match (rare — same order # used by different vendors),
    prefer the one whose sender_domain matches; fall back to vendor name.
    """
    rows = conn.execute(
        f"SELECT * FROM packages WHERE {column} IS NOT NULL AND {column} != ''"
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in rows:
        if _norm_id(row[column]) == target_norm:
            matches.append(dict(row))
    if not matches:
        return None
    if len(matches) > 1:
        if domain_norm:
            by_domain = [m for m in matches if _norm_domain(m.get("sender_domain")) == domain_norm]
            if by_domain:
                return by_domain[0]
        if vendor_norm:
            by_vendor = [m for m in matches if _norm_vendor(m.get("vendor")) == vendor_norm]
            if by_vendor:
                return by_vendor[0]
    return matches[0]


def _open_row_fallback(
    conn: sqlite3.Connection,
    domain_norm: str | None,
    vendor_norm: str | None,
) -> dict[str, Any] | None:
    """If exactly one recent open row matches by domain (preferred) or vendor, return it."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=VENDOR_FALLBACK_DAYS)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        """
        SELECT * FROM packages
        WHERE (tracking_number IS NULL OR tracking_number = '')
          AND created_at >= ?
          AND (status IS NULL OR status NOT IN ('delivered','cancelled','return_to_sender','failure','error'))
        """,
        (cutoff,),
    ).fetchall()
    if domain_norm:
        domain_matches = [
            dict(r) for r in rows if _norm_domain(r["sender_domain"]) == domain_norm
        ]
        if len(domain_matches) == 1:
            return domain_matches[0]
        if domain_matches:
            return None  # multiple same-domain → ambiguous, don't guess
    if vendor_norm:
        vendor_matches = [
            dict(r) for r in rows if _norm_vendor(r["vendor"]) == vendor_norm
        ]
        if len(vendor_matches) == 1:
            return vendor_matches[0]
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
