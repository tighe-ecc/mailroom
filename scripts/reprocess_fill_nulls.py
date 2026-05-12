"""One-shot: re-parse archived .eml files and fill NULL DB columns only.

Use when the email-parsing logic has improved (new fields, fixed
extraction) and you want to backfill rows without clobbering any manual
edits the user made in the dashboard.

Behavior:
- Walk every .eml under ~/Mailroom/.mailroom/processed/
- Re-parse with the current LLM prompt
- Use db.find_match to locate the existing row (does NOT create new rows)
- For each field where the row currently stores NULL and the parser
  produced a value, do a targeted UPDATE for that single column
- Skip rows that don't match (don't double-create), skip fields the user
  has already filled (preserves manual edits)

Reads OPENAI_API_KEY from .env. ~$0.03 per 30 emails.

Run: .venv/bin/python -m scripts.reprocess_fill_nulls
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow direct invocation: add the project root to sys.path so `mailroom` resolves.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mailroom import db, inbox, parser  # noqa: E402

log = logging.getLogger("reprocess")

# Columns we're willing to backfill. Excludes the bookkeeping columns
# (id, created_at, updated_at), polling-derived state (status, est_delivery,
# events, last_event*), and tracking_number (UNIQUE — collisions need the
# normal /update path to surface a useful error).
FILLABLE_COLUMNS = (
    "order_number",
    "description",
    "vendor",
    "po_number",
    "carrier",
    "ordered_date",
    "promised_ship_date",
    "promised_delivery_date",
    "tracking_url",
    "sender_domain",
)


def _resolve_ordered_date(parsed, email_date):
    if parsed.kind == "order_confirmation" and email_date:
        return email_date
    return parsed.ordered_date


def _resolve_delivery_estimate(parsed, anchor_date):
    if parsed.promised_delivery_date:
        return parsed.promised_delivery_date
    if not parsed.lead_time_days or not anchor_date:
        return None
    try:
        base = datetime.fromisoformat(anchor_date).date()
    except ValueError:
        return None
    return (base + timedelta(days=parsed.lead_time_days)).isoformat()


def _candidate_values(parsed, email_date, sender_domain):
    """What the current parser would write for this email, by column."""
    ordered_date = _resolve_ordered_date(parsed, email_date)
    promised_delivery_date = _resolve_delivery_estimate(parsed, ordered_date)
    return {
        "order_number": parsed.order_number,
        "description": parsed.item_description,
        "vendor": parsed.vendor,
        "po_number": parsed.po_number,
        "carrier": parsed.carrier,
        "ordered_date": ordered_date,
        "promised_ship_date": parsed.promised_ship_date,
        "promised_delivery_date": promised_delivery_date,
        "tracking_url": parsed.tracking_url,
        "sender_domain": sender_domain,
    }


def _fill_nulls(row, candidates):
    """Return {column: value} for columns where row is NULL and candidate is set."""
    patch = {}
    for col in FILLABLE_COLUMNS:
        existing = row.get(col)
        candidate = candidates.get(col)
        if not existing and candidate:
            patch[col] = candidate
    return patch


def _apply_patch(row_id: int, patch: dict) -> None:
    if not patch:
        return
    set_parts = [f"{c} = ?" for c in patch]
    params = list(patch.values()) + [row_id]
    with db.connect() as conn:
        conn.execute(
            f"UPDATE packages SET {', '.join(set_parts)}, updated_at = datetime('now') "
            f"WHERE id = ?",
            params,
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    db.init_schema()
    archive = inbox.inbox_dir() / inbox.INTERNAL_SUBDIR / "processed"
    paths = sorted(archive.glob("*.eml"))
    log.info("found %d archived .eml files", len(paths))

    seen = matched = filled = unmatched = errors = 0
    total_columns_filled = 0
    for path in paths:
        seen += 1
        try:
            subject, sender, body, email_date = inbox._load_eml(path)
            if not body:
                errors += 1
                log.warning("no body extracted: %s", path.name)
                continue
            parsed = parser.parse_email(subject, sender, body)
            if not parsed.is_actionable:
                log.info("not actionable: %s (kind=%s conf=%.2f)", path.name, parsed.kind, parsed.confidence)
                continue

            sender_domain = db.extract_sender_domain(sender)
            row = db.find_match(
                tracking_number=parsed.tracking_number,
                order_number=parsed.order_number,
                po_number=parsed.po_number,
                vendor=parsed.vendor,
                sender_domain=sender_domain,
            )
            if not row:
                unmatched += 1
                log.info("no matching row: %s", path.name)
                continue
            matched += 1
            candidates = _candidate_values(parsed, email_date, sender_domain)
            patch = _fill_nulls(row, candidates)
            if patch:
                filled += 1
                total_columns_filled += len(patch)
                log.info(
                    "row %s ← %s: %s",
                    row["id"], path.name, ", ".join(f"{k}={v!r}" for k, v in patch.items()),
                )
                _apply_patch(row["id"], patch)
        except Exception:
            errors += 1
            log.exception("failed: %s", path.name)

    log.info(
        "done: seen=%d matched=%d rows_filled=%d columns_filled=%d unmatched=%d errors=%d",
        seen, matched, filled, total_columns_filled, unmatched, errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
