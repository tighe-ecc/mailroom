"""Optional CLI for scripting: add-order, add-tracking, list, rm, poll, process-inbox."""

from __future__ import annotations

import typer

from . import db, easypost, inbox as inbox_mod, poll as poll_mod

app = typer.Typer(help="Mailroom CLI.")


@app.command("add-tracking")
def add_tracking(
    tracking_number: str = typer.Argument(..., help="Carrier tracking number."),
    description: str = typer.Option(..., "--desc", "-d", help="What are you expecting?"),
    vendor: str | None = typer.Option(None, "--vendor", "-v"),
    po_number: str | None = typer.Option(None, "--po"),
    order_number: str | None = typer.Option(None, "--order"),
    carrier: str | None = typer.Option(None, "--carrier", "-c"),
) -> None:
    """Register a tracking number and fetch its initial status."""
    db.init_schema()
    snap = easypost.create_tracker(tracking_number.strip(), carrier=carrier)
    row_id = db.add_package(
        tracking_number=snap.tracking_number,
        description=description,
        vendor=vendor,
        po_number=po_number,
        order_number=order_number,
        carrier=snap.carrier,
        easypost_id=snap.easypost_id,
        status=snap.status,
    )
    db.update_status(
        row_id=row_id,
        status=snap.status,
        est_delivery=snap.est_delivery,
        last_event=snap.last_event,
        last_event_time=snap.last_event_time,
        last_event_location=snap.last_event_location,
        events=snap.events,
        carrier=snap.carrier,
    )
    typer.echo(f"Added row {row_id}: {snap.tracking_number} ({snap.carrier or 'pending'}) — {snap.status}")


@app.command("add-order")
def add_order(
    description: str = typer.Option(..., "--desc", "-d"),
    vendor: str | None = typer.Option(None, "--vendor", "-v"),
    order_number: str | None = typer.Option(None, "--order"),
    po_number: str | None = typer.Option(None, "--po"),
    ordered_date: str | None = typer.Option(None, "--ordered", help="ISO date when placed."),
    promised_ship_date: str | None = typer.Option(None, "--ship-by"),
    promised_delivery_date: str | None = typer.Option(None, "--deliver-by"),
) -> None:
    """Register an order that has no tracking number yet."""
    db.init_schema()
    row_id = db.add_package(
        description=description,
        vendor=vendor,
        order_number=order_number,
        po_number=po_number,
        status="ordered",
        ordered_date=ordered_date,
        promised_ship_date=promised_ship_date,
        promised_delivery_date=promised_delivery_date,
    )
    typer.echo(f"Added order row {row_id}: {description} ({vendor or 'unknown vendor'})")


@app.command(name="list")
def list_cmd(
    include_delivered: bool = typer.Option(False, "--all", "-a"),
) -> None:
    """Print all tracked packages and orders."""
    db.init_schema()
    packages = db.list_packages(include_delivered=include_delivered)
    if not packages:
        typer.echo("Nothing tracked.")
        return
    for pkg in packages:
        identifier = pkg.get("tracking_number") or f"order:{pkg.get('order_number') or '—'}"
        typer.echo(
            f"[{easypost.display_status(pkg.get('status')):16}] "
            f"#{pkg['id']:<4} {identifier:25} "
            f"{(pkg.get('carrier') or '—'):8} "
            f"ETA {pkg.get('est_delivery') or pkg.get('promised_delivery_date') or '—':12} "
            f"— {pkg.get('description') or ''}"
        )


@app.command()
def rm(row_id: int = typer.Argument(..., help="Row id from `list`.")) -> None:
    """Remove a row from the tracker."""
    db.delete_package(row_id)
    typer.echo(f"Removed row {row_id}")


@app.command()
def poll() -> None:
    """Run one pass: ingest inbox, poll EasyPost, update rows."""
    summary = poll_mod.poll_once()
    typer.echo(f"poll complete: {summary}")


@app.command("process-inbox")
def process_inbox() -> None:
    """Parse every .eml file in the inbox folder."""
    summary = inbox_mod.process_inbox()
    typer.echo(f"inbox complete: {summary}")


if __name__ == "__main__":
    app()
