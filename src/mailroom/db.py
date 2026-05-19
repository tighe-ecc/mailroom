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

TERMINAL_STATUSES = {
    "delivered", "received", "cancelled", "return_to_sender", "failure", "error",
}

# Statuses hidden from the default dashboard view. "delivered" stays visible
# because the user still needs to walk to the mailroom rack and physically
# pick it up; only after they tick the "Received" checkbox does the row
# drop out of the active list.
HIDDEN_BY_DEFAULT_STATUSES = {
    "received", "cancelled", "return_to_sender", "failure", "error",
}

# Lifecycle ordering. Used to detect regressions — e.g. a late-arriving
# order-confirmation email should not downgrade an already-shipped row to
# "confirmed". Values are co-equal across "out_for_delivery" and
# "available_for_pickup" because they're parallel branches of the same
# in-flight stage. "received" sits one rank above "delivered" so the
# delivered → received transition (user picked it up from the mailroom rack)
# is treated as forward progress, not a regression.
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
    "received": 7,
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

        # po_number is the *buyer-side* PO (e.g. "FRD-249") that we put on
        # multiple vendors' orders for the same project — McMaster, Protolabs,
        # DigiKey all see the same FRD-249. So a po-number match alone is not
        # vendor identity; require sender_domain (or vendor name as a fallback)
        # to also agree, otherwise we'd splat a Protolabs order onto the
        # McMaster row that happens to share the PO.
        if norm_po:
            match = _scan_for_id(
                conn, "po_number", norm_po, norm_domain, norm_vendor,
                require_vendor_match=True,
            )
            if match:
                return match

        # Cross-field: vendors don't always agree on which is "order" vs "PO".
        # The same buyer-PO collision risk applies as soon as the po_number
        # column is involved on either side, so require a vendor/domain match
        # on the cross-field branches too.
        if norm_order:
            match = _scan_for_id(
                conn, "po_number", norm_order, norm_domain, norm_vendor,
                require_vendor_match=True,
            )
            if match:
                return match
        if norm_po:
            match = _scan_for_id(
                conn, "order_number", norm_po, norm_domain, norm_vendor,
                require_vendor_match=True,
            )
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
    require_vendor_match: bool = False,
) -> dict[str, Any] | None:
    """Find a row whose <column> normalizes to target_norm.

    When multiple rows match (rare — same order # used by different vendors),
    prefer the one whose sender_domain matches; fall back to vendor name.

    ``require_vendor_match`` tightens the match for ID columns that aren't
    vendor-unique on their own — most notably ``po_number``, which is the
    buyer-side PO and is reused across every vendor on a project. With it on,
    a candidate row whose sender_domain *and* vendor both disagree with the
    inbound email is rejected, even if it's the only po-number match.
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
    if require_vendor_match and (domain_norm or vendor_norm):
        for m in matches:
            row_domain = _norm_domain(m.get("sender_domain"))
            row_vendor = _norm_vendor(m.get("vendor"))
            domain_ok = domain_norm and row_domain and row_domain == domain_norm
            vendor_ok = vendor_norm and row_vendor and row_vendor == vendor_norm
            # Accept if at least one identifier agrees, or if the row simply
            # has no domain/vendor to compare against (legacy / manual rows).
            if domain_ok or vendor_ok or (not row_domain and not row_vendor):
                return m
        return None
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
        placeholders = ", ".join("?" for _ in HIDDEN_BY_DEFAULT_STATUSES)
        query = (
            f"SELECT * FROM packages WHERE status IS NULL OR status NOT IN ({placeholders})"
        )
        params = tuple(HIDDEN_BY_DEFAULT_STATUSES)
    # `(expr) IS NULL` emulates NULLS LAST: nulls sort to the bottom regardless of direction.
    query += f" ORDER BY ({expr}) IS NULL, {expr} {direction}, created_at DESC"
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_package(row_id: int, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM packages WHERE id = ?", (row_id,))


# Columns excluded from merge field-fill: identity, lifecycle timestamps, and
# the per-row tracker error (the destination row's error state is what's still
# valid; pulling a stale error from the deleted source would be misleading).
_MERGE_PROTECTED_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "tracker_error",
    "tracker_error_at",
}


def merge_packages(
    src_id: int,
    dst_id: int,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Combine two rows. Destination wins on any field it already has; missing
    fields are filled from the source. The source row is deleted.

    Returns ``(src_before, dst_before, dst_after)`` so callers can persist a
    full audit record — see :func:`log_merge`.
    """
    if src_id == dst_id:
        raise ValueError("cannot merge a row into itself")
    with connect(db_path) as conn:
        src = conn.execute("SELECT * FROM packages WHERE id = ?", (src_id,)).fetchone()
        dst = conn.execute("SELECT * FROM packages WHERE id = ?", (dst_id,)).fetchone()
        if src is None:
            raise ValueError(f"source row {src_id} not found")
        if dst is None:
            raise ValueError(f"destination row {dst_id} not found")
        src_d = dict(src)
        dst_d = dict(dst)

        merged = dict(dst_d)
        for col, src_val in src_d.items():
            if col in _MERGE_PROTECTED_COLUMNS:
                continue
            if not merged.get(col) and src_val:
                merged[col] = src_val

        # Delete src first so its tracking_number frees the UNIQUE slot before
        # we (potentially) write the same value onto dst.
        conn.execute("DELETE FROM packages WHERE id = ?", (src_id,))

        update_cols = [
            c for c in merged
            if c not in {"id", "created_at"} and merged[c] != dst_d.get(c)
        ]
        if update_cols:
            merged["updated_at"] = _now()
            if "updated_at" not in update_cols:
                update_cols.append("updated_at")
            set_clause = ", ".join(f"{c} = ?" for c in update_cols)
            params = [merged[c] for c in update_cols] + [dst_id]
            conn.execute(
                f"UPDATE packages SET {set_clause} WHERE id = ?", params
            )

    return src_d, dst_d, merged


def merge_log_path(db_path: Path | None = None) -> Path:
    """Where manual-merge audit records are appended (JSONL)."""
    return (db_path or default_db_path()).parent / "merges.jsonl"


def log_merge(
    src_before: dict[str, Any],
    dst_before: dict[str, Any],
    merged_after: dict[str, Any],
    db_path: Path | None = None,
) -> None:
    """Append a JSONL audit record so we can later analyze why find_match
    didn't catch this duplicate and how to improve it."""
    path = merge_log_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _now(),
        "src": src_before,
        "dst": dst_before,
        "merged": merged_after,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str))
        f.write("\n")


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
