"""Fallback tracking via scraping vendor/carrier tracking pages.

Used when EasyPost can't help (unsupported carrier, freight, vendor-hosted
portal). Fetches the page's HTML, flattens it to text, and asks an LLM to
pull out the same fields EasyPost would give us. Works on server-rendered
pages; JS-only single-page apps (Amazon, some carrier portals) will come
back with low confidence because the initial HTML has no data.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import html2text
from dotenv import load_dotenv
from openai import OpenAI

from . import db, notify


log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
FETCH_TIMEOUT = 15
MAX_CONTENT_CHARS = 16_000
MIN_CONFIDENCE = 0.4

VALID_STATUSES = [
    "ordered", "confirmed", "in_fulfillment",
    "pre_transit", "in_transit", "out_for_delivery",
    "delivered", "available_for_pickup",
    "return_to_sender", "failure", "cancelled", "error", "unknown",
]

# Re-export for legacy in-module use; canonical source is db.STATUS_RANK so the
# inbox pipeline and scrape pipeline agree on lifecycle ordering.
_STATUS_RANK = db.STATUS_RANK


SYSTEM_PROMPT = f"""\
You extract parcel/freight shipment tracking info from carrier and vendor
tracking web pages.

Respond with a single JSON object matching this schema exactly:
{{
  "status": one of {VALID_STATUSES},
  "est_delivery": string | null,         // ISO date YYYY-MM-DD if a delivery date is given
  "last_event": string | null,           // short phrase for the most recent tracking event
  "last_event_time": string | null,      // ISO timestamp if present, else YYYY-MM-DD
  "last_event_location": string | null,  // "city, state" or similar
  "events": [
     {{
       "status": string | null,
       "message": string | null,
       "datetime": string | null,
       "location": string | null
     }}
  ],
  "confidence": number                   // 0.0 to 1.0, how sure you are the extraction is accurate
}}

Rules:
- Only use info actually present in the page text. If a field is not present, null.
- Do not invent dates. "04/22/2026" becomes "2026-04-22".
- Status mapping hints:
  "pre_transit": label created, carrier hasn't picked up yet
  "in_transit":  shipped, on the way (no delivery today signal)
  "out_for_delivery": on truck for delivery today
  "delivered": delivered
- If the page is a login wall, error, or contains no tracking info: status="unknown"
  and confidence<=0.2.
"""


@dataclass
class ScrapedSnapshot:
    status: str
    est_delivery: str | None
    last_event: str | None
    last_event_time: str | None
    last_event_location: str | None
    events: list[dict[str, Any]]
    confidence: float


def _unwrap_safelinks(url: str) -> str:
    """Strip Microsoft Office 365 SafeLinks wrappers so we fetch the real destination."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if "safelinks.protection" in host or "safelinks.office" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        inner = qs.get("url", [None])[0]
        if inner:
            return urllib.parse.unquote(inner)
    return url


def _fetch_text(url: str) -> str:
    url = _unwrap_safelinks(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    h2t = html2text.HTML2Text()
    h2t.ignore_images = True
    h2t.ignore_links = True
    h2t.body_width = 0
    text = h2t.handle(body)
    return text[:MAX_CONTENT_CHARS]


def _client() -> OpenAI:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set. Add it to .env before scraping.")
    return OpenAI(api_key=key)


def _coerce(payload: dict[str, Any]) -> ScrapedSnapshot:
    status = str(payload.get("status") or "unknown").strip().lower()
    if status not in VALID_STATUSES:
        status = "unknown"

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    events: list[dict[str, Any]] = []
    raw_events = payload.get("events")
    if isinstance(raw_events, list):
        for e in raw_events[:20]:
            if isinstance(e, dict):
                events.append({
                    "status": e.get("status"),
                    "message": e.get("message"),
                    "datetime": e.get("datetime"),
                    "location": e.get("location"),
                })

    def _s(k: str) -> str | None:
        v = payload.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    return ScrapedSnapshot(
        status=status,
        est_delivery=_s("est_delivery"),
        last_event=_s("last_event"),
        last_event_time=_s("last_event_time"),
        last_event_location=_s("last_event_location"),
        events=events,
        confidence=confidence,
    )


def scrape(url: str) -> ScrapedSnapshot:
    """Fetch `url` and ask the LLM to extract tracking info. Raises on fetch errors."""
    text = _fetch_text(url)
    response = _client().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"URL: {url}\n\n{text}"},
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("scrape parser returned non-JSON: %r", raw[:200])
        payload = {}
    return _coerce(payload)


# ---------- Carrier-specific handlers ----------
#
# Some carriers (e.g. 4PX) publish their tracking through JS-only SPAs that a plain
# fetch can't render. For those we call the backing API directly. The generic
# LLM scraper remains the fallback for any carrier we don't have a handler for.

def _query_4px(tracking_number: str) -> ScrapedSnapshot:
    """Fetch tracking info for a 4PX shipment via their public listTrackV3 API."""
    req = urllib.request.Request(
        "https://track.4px.com/track/v2/front/listTrackV3",
        data=json.dumps({"queryCodes": [tracking_number], "language": "en-us"}).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://track.4px.com/",
            "Origin": "https://track.4px.com",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))

    data = payload.get("data") or []
    if not data:
        return ScrapedSnapshot("unknown", None, None, None, None, [], 0.0)
    item = data[0]
    raw_tracks = item.get("tracks") or []

    events: list[dict[str, Any]] = []
    for t in raw_tracks:
        events.append({
            "status": t.get("tkCategoryName"),
            "message": t.get("tkTranslatedDesc") or t.get("tkDesc"),
            "datetime": t.get("tkDate"),
            "location": (t.get("tkLocation") or None),
        })

    # 4PX returns tracks newest-first. Derive status from the latest event's
    # description and category. Note: tkCategoryCode "D" means "Transiting in
    # Destination Country" (e.g. customs cleared, out for delivery), NOT
    # delivered — so we key delivery off the description text.
    status = "in_transit" if events else "pre_transit"
    last = raw_tracks[0] if raw_tracks else {}
    cat = (last.get("tkCategoryCode") or "").upper()
    desc = (last.get("tkTranslatedDesc") or last.get("tkDesc") or "").lower()
    if "out for delivery" in desc or "with delivery courier" in desc:
        status = "out_for_delivery"
    elif "delivered" in desc or "signed for" in desc or "signed by" in desc:
        status = "delivered"
    elif cat == "R":
        status = "return_to_sender"
    elif cat in {"O", "L"} and not any(
        (t.get("tkCategoryCode") or "").upper() in {"M", "C", "A", "S", "I", "D"}
        for t in raw_tracks
    ):
        status = "pre_transit"

    last_event = events[0]["message"] if events else None
    last_event_time = events[0]["datetime"] if events else None
    last_event_location = events[0]["location"] if events else None

    return ScrapedSnapshot(
        status=status,
        est_delivery=None,
        last_event=last_event,
        last_event_time=last_event_time,
        last_event_location=last_event_location,
        events=events,
        confidence=0.9 if events else 0.2,
    )


def _looks_like_4px(pkg: dict[str, Any]) -> bool:
    carrier = (pkg.get("carrier") or "").strip().lower()
    if carrier == "4px":
        return True
    tn = (pkg.get("tracking_number") or "").upper()
    return tn.startswith("4PX")


# Per-carrier public tracking URL templates. Keyed by lowercased carrier name;
# "{tn}" is substituted with the url-encoded tracking number.
_CARRIER_URL_TEMPLATES = {
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={tn}",
    "ups": "https://www.ups.com/track?tracknum={tn}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tn}",
    "dhl": "https://www.dhl.com/en/express/tracking.html?AWB={tn}",
    "dhlexpress": "https://www.dhl.com/en/express/tracking.html?AWB={tn}",
    "ontrac": "https://www.ontrac.com/tracking?number={tn}",
    "4px": "https://track.4px.com/#/result/0/{tn}/en-us",
}

# Host suffixes we consider trustworthy for a given carrier. If a stored
# tracking URL's host matches one of these, we believe the email gave us a
# real deep-link. If not — and we have a template for the carrier — the
# template wins. This guards against parser hallucinations (e.g. a
# Protolabs shipping email whose tracking_url got filled in with a
# d.digikey.com click-tracker pulled from a previous DigiKey order) and
# against generic landing pages (e.g. a bare https://www.fedex.com/apps/
# fedextrack/ with no tracking number in the query string).
_CARRIER_TRUSTED_HOSTS: dict[str, tuple[str, ...]] = {
    "fedex": ("fedex.com",),
    "ups": ("ups.com",),
    "usps": ("usps.com",),
    "dhl": ("dhl.com",),
    "dhlexpress": ("dhl.com",),
    "ontrac": ("ontrac.com",),
    "4px": ("4px.com",),
}


def _normalize_carrier(raw: str | None) -> str:
    return (raw or "").strip().lower().replace(" ", "")


def _sniff_carrier_from_tracking_number(tn: str) -> str | None:
    """Best-effort carrier guess from a tracking-number format.

    Used when the stored carrier field is missing/unrecognized. Patterns are
    intentionally conservative — we'd rather return None and fall back than
    deep-link a UPS number into FedEx.
    """
    upper = tn.upper().strip()
    compact = upper.replace(" ", "")
    if not compact:
        return None
    if compact.startswith("1Z") and len(compact) == 18:
        return "ups"
    if compact.startswith("4PX"):
        return "4px"
    # FedEx Ground/Express/SmartPost: 12, 15, 20, or 22 digits. Many UPS Mail
    # Innovations / USPS numbers are also long-digit, so only claim FedEx for
    # the canonical Express/Ground 12-digit length to avoid false positives.
    if compact.isdigit() and len(compact) == 12:
        return "fedex"
    return None


def _stored_url_matches_carrier(stored: str, carrier_key: str) -> bool:
    """True iff `stored` looks like a real deep-link for `carrier_key`.

    Returns True when we can't decide — e.g. there's no trusted-host list for
    the carrier — so non-major carriers (Chitchats, OnTrac variants, vendor
    portals like uline.com) keep working as they did before.
    """
    trusted = _CARRIER_TRUSTED_HOSTS.get(carrier_key)
    if not trusted:
        return True
    try:
        host = (urllib.parse.urlparse(stored).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in trusted)


def tracking_url_for(pkg: dict[str, Any]) -> str | None:
    """Pick the best user-visible tracking URL for a package row.

    Priority:
      1. The URL captured from the shipping email *if* it looks plausible for
         this package's carrier (host matches a known-good host for that
         carrier).
      2. A known-carrier template derived from the carrier field, or sniffed
         from the tracking-number format when the carrier field is missing.
      3. The stored URL as-is, if we have no better option.
      4. None.
    """
    tn = pkg.get("tracking_number")
    stored = pkg.get("tracking_url")
    carrier_key = _normalize_carrier(pkg.get("carrier"))

    if not carrier_key and tn:
        carrier_key = _sniff_carrier_from_tracking_number(tn) or ""

    tmpl = _CARRIER_URL_TEMPLATES.get(carrier_key) if carrier_key else None
    if not tmpl and tn and tn.upper().startswith("4PX"):
        tmpl = _CARRIER_URL_TEMPLATES["4px"]
        carrier_key = carrier_key or "4px"

    if stored:
        if not carrier_key or _stored_url_matches_carrier(stored, carrier_key):
            return stored
        # Stored URL's host disagrees with the carrier (e.g. a hallucinated
        # d.digikey.com link on a FedEx Protolabs shipment). Prefer the
        # carrier template when we have one; otherwise fall through to the
        # stored URL as a last resort.
        if tmpl and tn:
            return tmpl.format(tn=urllib.parse.quote(tn, safe=""))
        return stored

    if not tn:
        return None
    if not tmpl:
        return None
    return tmpl.format(tn=urllib.parse.quote(tn, safe=""))


def try_tracking(pkg: dict[str, Any]) -> ScrapedSnapshot | None:
    """Pick the best tracking source for this package and return a snapshot (or None)."""
    if _looks_like_4px(pkg) and pkg.get("tracking_number"):
        try:
            return _query_4px(pkg["tracking_number"])
        except Exception:
            log.exception("4PX API call failed for row %s", pkg.get("id"))

    url = pkg.get("tracking_url")
    if url:
        try:
            return scrape(url)
        except Exception:
            log.exception("generic scrape failed for row %s (%s)", pkg.get("id"), url)

    return None


def apply_to_row(
    pkg: dict[str, Any],
    *,
    old_status: str | None = None,
    description: str | None = None,
) -> bool:
    """Try all tracking sources for this row and write the best result to the DB."""
    row_id = pkg["id"]
    snap = try_tracking(pkg)
    if snap is None:
        db.set_tracker_error(row_id, "no tracking source available")
        return False

    if snap.confidence < MIN_CONFIDENCE or snap.status == "unknown":
        log.info(
            "scrape returned low-confidence result for row %s (conf=%.2f, status=%s)",
            row_id, snap.confidence, snap.status,
        )
        return False

    effective_status = snap.status
    if old_status and _STATUS_RANK.get(snap.status, -1) < _STATUS_RANK.get(old_status, -1):
        effective_status = old_status

    db.update_status(
        row_id=row_id,
        status=effective_status,
        est_delivery=snap.est_delivery,
        last_event=snap.last_event,
        last_event_time=snap.last_event_time,
        last_event_location=snap.last_event_location,
        events=snap.events,
    )

    if old_status is not None and effective_status != old_status:
        notify.notify_status_change(
            description=description or "",
            old_status=old_status,
            new_status=effective_status,
            location=snap.last_event_location,
            vendor=pkg.get("vendor"),
        )

    return True
