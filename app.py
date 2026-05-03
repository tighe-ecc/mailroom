"""FastAPI + Tailwind/DaisyUI dashboard for Mailroom.

Run: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 47821
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import re

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mailroom import db, easypost, inbox, poll, scrape, watcher

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedback import note as _feedback_note  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")


STATUS_BADGE = {
    "ordered": "ghost",
    "confirmed": "ghost",
    "in_fulfillment": "ghost",
    "pre_transit": "info",
    "in_transit": "info",
    "out_for_delivery": "warning",
    "delivered": "success",
    "available_for_pickup": "info",
    "return_to_sender": "error",
    "failure": "error",
    "cancelled": "error",
    "error": "error",
    "unknown": "ghost",
}


def _pretty_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%a %b %-d")
    except ValueError:
        return value


def _pretty_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %-I:%M %p")
    except ValueError:
        return value


def _status_class(status: str | None) -> str:
    return STATUS_BADGE.get(status or "unknown", "ghost")


templates.env.filters["pretty_date"] = _pretty_date
templates.env.filters["pretty_time"] = _pretty_time
templates.env.filters["status_class"] = _status_class
templates.env.filters["display_status"] = easypost.display_status
templates.env.filters["tracking_url_for"] = scrape.tracking_url_for


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_schema()
    observer = watcher.start()
    try:
        yield
    finally:
        watcher.stop(observer)


app = FastAPI(title="Mailroom", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.post("/feedback")
async def feedback(request: Request) -> dict[str, bool]:
    payload = await request.json()
    _feedback_note(
        payload.get("description", ""),
        type=payload.get("type", "bug"),
        title=payload.get("title") or None,
        tool=payload.get("tool") or None,
        url=payload.get("url") or None,
        path=ROOT,
    )
    return {"ok": True}


def _context(
    request: Request,
    include_delivered: bool = False,
    sort: str = "last_event_time",
    dir: str = "desc",
) -> dict[str, Any]:
    if sort not in db.SORTABLE_COLUMNS:
        sort = "last_event_time"
    if dir.lower() not in {"asc", "desc"}:
        dir = "desc"
    packages = db.list_packages(
        include_delivered=include_delivered, sort_by=sort, sort_dir=dir
    )
    counts = db.status_counts()
    return {
        "packages": packages,
        "counts": counts,
        "pre_shipment_count": db.pre_shipment_count(),
        "inbox_pending": inbox.pending_count(),
        "include_delivered": include_delivered,
        "sort_by": sort,
        "sort_dir": dir.lower(),
        "is_htmx": request.headers.get("hx-request") == "true",
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _context(request))


@app.get("/packages", response_class=HTMLResponse)
def packages_fragment(
    request: Request,
    include_delivered: bool = False,
    sort: str = "last_event_time",
    dir: str = "desc",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_packages.html", _context(request, include_delivered, sort, dir)
    )


@app.get("/packages/{row_id}/details", response_class=HTMLResponse)
def package_detail(request: Request, row_id: int) -> HTMLResponse:
    import json as _json
    pkg = db.get_package(row_id)
    if pkg is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)
    events: list[dict] = []
    if pkg.get("events_json"):
        try:
            events = _json.loads(pkg["events_json"])
        except _json.JSONDecodeError:
            events = []
    return templates.TemplateResponse(
        request, "_detail.html", {"pkg": pkg, "events": events}
    )


def _nn(s: str) -> str | None:
    s = s.strip()
    return s or None


@app.post("/add", response_class=HTMLResponse)
def add_row(
    request: Request,
    description: str = Form(...),
    tracking_number: str = Form(""),
    order_number: str = Form(""),
    vendor: str = Form(""),
    po_number: str = Form(""),
    carrier: str = Form(""),
    ordered_date: str = Form(""),
    promised_ship_date: str = Form(""),
    promised_delivery_date: str = Form(""),
) -> HTMLResponse:
    description = description.strip()
    tracking_number = tracking_number.strip()
    easypost_id = None
    status = "ordered"
    final_carrier = _nn(carrier)

    if tracking_number:
        snap = easypost.create_tracker(tracking_number, carrier=final_carrier)
        tracking_number = snap.tracking_number
        final_carrier = snap.carrier or final_carrier
        easypost_id = snap.easypost_id
        status = snap.status or "pre_transit"

    row_id = db.add_package(
        tracking_number=tracking_number or None,
        description=description,
        vendor=_nn(vendor),
        po_number=_nn(po_number),
        order_number=_nn(order_number),
        carrier=final_carrier,
        easypost_id=easypost_id,
        status=status,
        ordered_date=_nn(ordered_date),
        promised_ship_date=_nn(promised_ship_date),
        promised_delivery_date=_nn(promised_delivery_date),
    )

    if tracking_number and easypost_id:
        snap = easypost.retrieve_tracker(easypost_id)
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

    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/packages/{row_id}/update", response_class=HTMLResponse)
def update_row(
    request: Request,
    row_id: int,
    description: str = Form(""),
    vendor: str = Form(""),
    order_number: str = Form(""),
    po_number: str = Form(""),
    tracking_number: str = Form(""),
    carrier: str = Form(""),
    status: str = Form(""),
    ordered_date: str = Form(""),
    promised_ship_date: str = Form(""),
    promised_delivery_date: str = Form(""),
) -> HTMLResponse:
    existing = db.get_package(row_id)
    if existing is None:
        return HTMLResponse("<p class='text-error'>Not found.</p>", status_code=404)

    new_tracking = tracking_number.strip() or None
    easypost_id = existing.get("easypost_id")
    final_carrier = _nn(carrier) or existing.get("carrier")
    final_status = _nn(status) or existing.get("status")
    had_tracking_before = bool(existing.get("tracking_number"))

    if new_tracking and not had_tracking_before:
        try:
            snap = easypost.create_tracker(new_tracking, carrier=final_carrier)
            easypost_id = snap.easypost_id
            final_carrier = snap.carrier or final_carrier
            final_status = snap.status or final_status
        except Exception:
            logging.exception("EasyPost tracker creation failed for %s", new_tracking)

    db.update_package(
        row_id=row_id,
        description=_nn(description),
        vendor=_nn(vendor),
        order_number=_nn(order_number),
        po_number=_nn(po_number),
        tracking_number=new_tracking,
        carrier=final_carrier,
        status=final_status,
        easypost_id=easypost_id,
        ordered_date=_nn(ordered_date),
        promised_ship_date=_nn(promised_ship_date),
        promised_delivery_date=_nn(promised_delivery_date),
    )

    # Return refreshed detail fragment (swaps into #detail-content in the modal).
    return package_detail(request, row_id)


@app.delete("/packages/{row_id}", response_class=HTMLResponse)
def delete_row(request: Request, row_id: int) -> HTMLResponse:
    db.delete_package(row_id)
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/packages/{src_id}/merge/{dst_id}", response_class=HTMLResponse)
def merge_rows(request: Request, src_id: int, dst_id: int) -> HTMLResponse:
    """Combine two rows (drag-and-drop in the dashboard) and append a JSONL
    audit record so we can later analyze missed auto-dedup cases."""
    if src_id == dst_id:
        return HTMLResponse("cannot merge a row into itself", status_code=400)
    try:
        src_before, dst_before, merged = db.merge_packages(src_id, dst_id)
    except ValueError as e:
        return HTMLResponse(str(e), status_code=404)
    db.log_merge(src_before, dst_before, merged)
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request) -> HTMLResponse:
    poll.poll_once()
    return templates.TemplateResponse(request, "_packages.html", _context(request))


@app.post("/inbox/process", response_class=HTMLResponse)
def process_inbox(request: Request) -> HTMLResponse:
    inbox.process_inbox()
    return templates.TemplateResponse(request, "_packages.html", _context(request))


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9 ._\-()#&]+")


@app.post("/inbox/upload", response_class=HTMLResponse)
async def upload_eml(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
    name = Path(file.filename or "upload.eml").name
    if not name.lower().endswith(".eml"):
        name = f"{name}.eml"
    name = _SAFE_FILENAME.sub("_", name)[:200] or "upload.eml"

    destination = inbox.inbox_dir() / name
    # If a file with the same name already exists, append a counter.
    if destination.exists():
        stem, suffix = destination.stem, destination.suffix
        i = 2
        while (candidate := destination.with_name(f"{stem} ({i}){suffix}")).exists():
            i += 1
        destination = candidate

    destination.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    destination.write_bytes(content)

    inbox.process_inbox()
    return templates.TemplateResponse(request, "_packages.html", _context(request))
