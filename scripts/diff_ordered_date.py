"""Show rows whose ordered_date disagrees with their source email's Date header.

These are the specific old-LLM mistakes the new "email header wins for
order_confirmation" rule was added to fix (StepperOnline "Sun Jul 5" case).
Dry-run by default — pass --apply to actually overwrite.

Only touches order_confirmation emails. Shipping confirmations are skipped:
their Date header is the ship date, not the order date.

Run:
  .venv/bin/python -m scripts.diff_ordered_date         # dry-run
  .venv/bin/python -m scripts.diff_ordered_date --apply # commit changes
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mailroom import db, inbox, parser  # noqa: E402


def main(apply: bool) -> int:
    db.init_schema()
    archive = inbox.inbox_dir() / inbox.INTERNAL_SUBDIR / "processed"
    paths = sorted(archive.glob("*.eml"))
    print(f"scanning {len(paths)} archived .eml files\n")

    proposals = []
    for path in paths:
        try:
            subject, sender, body, email_date = inbox._load_eml(path)
            if not body or not email_date:
                continue
            parsed = parser.parse_email(subject, sender, body)
            if parsed.kind != "order_confirmation" or not parsed.is_actionable:
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
                continue
            current = row.get("ordered_date")
            if current and current != email_date:
                proposals.append({
                    "row_id": row["id"],
                    "vendor": row.get("vendor"),
                    "order_number": row.get("order_number"),
                    "current": current,
                    "proposed": email_date,
                    "eml": path.name,
                })
        except Exception as e:
            print(f"  ERROR on {path.name}: {e}")

    if not proposals:
        print("No discrepancies found.")
        return 0

    print(f"{'row':<5} {'vendor':<22} {'order#':<14} {'current':<12} → {'proposed':<12}  source")
    print("-" * 110)
    for p in proposals:
        print(
            f"{p['row_id']:<5} {(p['vendor'] or '—')[:21]:<22} "
            f"{(p['order_number'] or '—')[:13]:<14} {p['current']:<12} → "
            f"{p['proposed']:<12}  {p['eml']}"
        )

    print(f"\n{len(proposals)} row(s) would change.")
    if apply:
        with db.connect() as conn:
            for p in proposals:
                conn.execute(
                    "UPDATE packages SET ordered_date = ?, updated_at = datetime('now') WHERE id = ?",
                    (p["proposed"], p["row_id"]),
                )
        print("Applied.")
    else:
        print("Dry-run only. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
