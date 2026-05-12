"""OpenAI-backed extractor for order and shipping confirmation emails.

Takes a parsed email's subject + sender + body, returns a structured
`ParsedEmail` record the inbox pipeline uses to create or update a row.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"
MIN_CONFIDENCE = 0.4


SYSTEM_PROMPT = """\
You extract structured data from procurement-related emails (order confirmations \
and shipping confirmations) for a small manufacturing shop.

Respond with a single JSON object matching this schema exactly:
{
  "kind": "order_confirmation" | "shipping_confirmation" | "unknown",
  "vendor": string | null,             // human name, e.g. "McMaster-Carr", "Amazon", "Grainger"
  "order_number": string | null,        // vendor's order/invoice/reference #
  "po_number": string | null,           // buyer-side PO if one appears
  "item_description": string | null,    // short human summary of what was ordered
  "ordered_date": string | null,        // YYYY-MM-DD when possible
  "promised_ship_date": string | null,  // YYYY-MM-DD
  "promised_delivery_date": string | null, // YYYY-MM-DD
  "lead_time_days": number | null,      // estimated total calendar days from order to delivery, when the email gives only a relative phrase (e.g. "6-8 weeks" -> 56, "ships in 3 business days" -> 5). Use the upper bound of any range. Null when an absolute promised_delivery_date is given.
  "tracking_number": string | null,     // carrier tracking # if present in email
  "carrier": string | null,             // "UPS", "FedEx", "USPS", "DHLExpress", "AmazonMws", etc.
  "tracking_url": string | null,        // direct carrier/vendor tracking URL from the email, if present. Prefer the raw destination URL over Microsoft safelinks wrappers.
  "status_signal": "ordered" | "confirmed" | "in_fulfillment" | "shipped" | null,
  "confidence": number,                 // 0.0 to 1.0, your self-assessed confidence
  "notes": string | null                // optional: short reasoning if something is ambiguous
}

Rules:
- If the email is not a purchase-order or shipping email, set kind="unknown" and \
confidence<=0.2; leave other fields null.
- "confirmed" = vendor acknowledged receipt of the order (no shipping info yet).
- "in_fulfillment" = vendor is preparing the shipment (picking/packing).
- "shipped" = a tracking number and/or carrier handoff is mentioned.
- Prefer shipping_confirmation over order_confirmation if both signals are present.
- Dates should be ISO format. If only a relative phrase is given ("ships in 3 \
business days", "6-8 weeks lead time"), leave the date fields null and populate \
lead_time_days instead so the dashboard can compute an estimated delivery date.
- Do not invent tracking numbers or dates. If unsure, use null.
- Keep item_description under ~80 characters.
"""


@dataclass
class ParsedEmail:
    kind: str
    vendor: str | None
    order_number: str | None
    po_number: str | None
    item_description: str | None
    ordered_date: str | None
    promised_ship_date: str | None
    promised_delivery_date: str | None
    lead_time_days: int | None
    tracking_number: str | None
    carrier: str | None
    tracking_url: str | None
    status_signal: str | None
    confidence: float
    notes: str | None

    @property
    def is_actionable(self) -> bool:
        return self.kind != "unknown" and self.confidence >= MIN_CONFIDENCE


def _client() -> OpenAI:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Add it to .env before processing emails."
        )
    return OpenAI(api_key=key)


def _coerce(payload: dict[str, Any]) -> ParsedEmail:
    def _str(k: str) -> str | None:
        v = payload.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    kind = _str("kind") or "unknown"
    if kind not in {"order_confirmation", "shipping_confirmation", "unknown"}:
        kind = "unknown"

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    status_signal = _str("status_signal")
    if status_signal not in {"ordered", "confirmed", "in_fulfillment", "shipped", None}:
        status_signal = None

    lead_time_days: int | None
    raw_lead = payload.get("lead_time_days")
    try:
        lead_time_days = int(raw_lead) if raw_lead is not None else None
    except (TypeError, ValueError):
        lead_time_days = None
    if lead_time_days is not None and (lead_time_days <= 0 or lead_time_days > 365):
        lead_time_days = None

    return ParsedEmail(
        kind=kind,
        vendor=_str("vendor"),
        order_number=_str("order_number"),
        po_number=_str("po_number"),
        item_description=_str("item_description"),
        ordered_date=_str("ordered_date"),
        promised_ship_date=_str("promised_ship_date"),
        promised_delivery_date=_str("promised_delivery_date"),
        lead_time_days=lead_time_days,
        tracking_number=_str("tracking_number"),
        carrier=_str("carrier"),
        tracking_url=_str("tracking_url"),
        status_signal=status_signal,
        confidence=confidence,
        notes=_str("notes"),
    )


def parse_email(subject: str, sender: str, body: str) -> ParsedEmail:
    """Ask the LLM to classify and extract fields from an email."""
    # Trim aggressively: 12k chars is ~3k tokens, enough for even long shipping emails.
    trimmed_body = body[:12_000]

    user_content = (
        f"Subject: {subject}\nFrom: {sender}\n\n{trimmed_body}"
    )

    response = _client().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("parser returned non-JSON payload: %r", raw[:200])
        payload = {}

    parsed = _coerce(payload)
    log.info("parsed email: %s", asdict(parsed))
    return parsed
