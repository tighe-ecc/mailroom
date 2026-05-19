"""OpenAI-backed extractor for order and shipping confirmation emails.

Takes a parsed email's subject + sender + body, returns a structured
`ParsedEmail` record the inbox pipeline uses to create or update a row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


log = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"
MIN_CONFIDENCE = 0.4

# Bumped manually when we want everything re-parsed on the next startup scan,
# even if the parser source hasn't visibly changed (e.g. an OpenAI model
# upgrade we want to backfill onto historical .emls). Combined with a short
# content hash of parser.py to form effective_parser_version() — that's what
# gets stored in email_files.parser_version and compared on each scan.
#
# Invariant: every file whose change could affect parse output MUST be hashed
# in _parser_content_hash(). Today the entire grounding + LLM call lives in
# this file (parser.py), and SYSTEM_PROMPT is a constant inside it, so hashing
# parser.py alone is sufficient. If grounding logic moves into another module
# in the future, add that file to the hash here.
PARSER_VERSION = "v1"


def _parser_content_hash() -> str:
    """Short content hash that catches substantive parser/prompt changes the
    PARSER_VERSION bump might forget. See PARSER_VERSION docstring for the
    invariant."""
    h = hashlib.sha256()
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()[:10]


def effective_parser_version() -> str:
    """The version string written to ``email_files.parser_version`` on every
    parse. Compared exact-string on the next startup scan to decide whether
    a .eml needs to be re-ingested."""
    return f"{PARSER_VERSION}+{_parser_content_hash()}"


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
  "ordered_date": string | null,        // YYYY-MM-DD when possible — the date the order was actually placed, as stated in the email body
  "ordered_date_confidence": number,    // 0.0 to 1.0, how sure you are that ordered_date is the *order placement date* (not a promised-ship date, a quote-validity date, or a customer-since date). Be conservative: 0.95+ only when the body explicitly labels the date as the order date ("Order date:", "Placed on:", "Order placed: …"). Use ≤0.5 when you inferred the date from a header like "Date received:" or any field whose role you're unsure of.
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
- GROUNDING (critical): every value you place in tracking_number, tracking_url, \
carrier, order_number, and po_number MUST be copyable verbatim from the email \
body or headers shown to you. If the email body does not contain the literal \
string for a field, return null for that field. Do not synthesize a tracking \
URL from a tracking number unless the URL itself is printed in the email — the \
downstream pipeline can build canonical carrier URLs on its own. Do not pattern- \
match a tracking number into a carrier name; only fill carrier when the email \
text names the carrier.
- If you're tempted to "fill in" a field based on what a similar email usually \
contains, return null instead. The downstream pipeline prefers a null field \
over a guessed one.
- If a single-value field has multiple candidates in the email (e.g. a \
multi-package shipment with two tracking numbers), return only the FIRST one \
as it appears in the email. Do not join values with commas. The pipeline \
stores one tracking number per row; surface the others in `notes` if useful.
- Keep item_description under ~80 characters.
- ordered_date_confidence is required even when ordered_date is null (use 0.0). \
The downstream pipeline falls back to the email Date header when this confidence \
is below a threshold, so honest low scores are valuable — don't anchor at 0.9.
"""


@dataclass
class ParsedEmail:
    kind: str
    vendor: str | None
    order_number: str | None
    po_number: str | None
    item_description: str | None
    ordered_date: str | None
    ordered_date_confidence: float
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

    try:
        ordered_date_confidence = float(payload.get("ordered_date_confidence") or 0.0)
    except (TypeError, ValueError):
        ordered_date_confidence = 0.0
    ordered_date_confidence = max(0.0, min(1.0, ordered_date_confidence))

    return ParsedEmail(
        kind=kind,
        vendor=_str("vendor"),
        order_number=_str("order_number"),
        po_number=_str("po_number"),
        item_description=_str("item_description"),
        ordered_date=_str("ordered_date"),
        ordered_date_confidence=ordered_date_confidence,
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


_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    """Lowercase + strip non-alphanumerics. Lets us substring-test across HTML
    artifacts, soft-wraps, and minor formatting differences without changing
    the displayed value."""
    return _ALNUM_RE.sub("", s.lower())


def _appears_in(value: str, haystack_norm: str) -> bool:
    """True iff `value` appears in `haystack_norm` once both are normalized."""
    v = _normalize(value)
    return bool(v) and v in haystack_norm


_MULTI_VALUE_SPLIT_RE = re.compile(r"[,;]|\sand\s", re.IGNORECASE)


def _first_grounded(value: str, haystack_norm: str) -> str | None:
    """Return the first sub-value of `value` that appears verbatim in the body.

    Models occasionally concatenate multiple values into a single string when
    the schema accepts only one — e.g. a multi-package shipment with two
    tracking numbers comes back as "794612345678, 794612345679". The naive
    substring check then rejects the whole joined string and the user loses
    both values. This helper:

      1. tries the whole value first (the common case is a single scalar that's
         already verbatim in the body);
      2. otherwise splits on multi-value separators (comma, semicolon, " and ")
         and returns the first part that appears verbatim.

    Returns None if no part is grounded. Stays vendor-agnostic — the same
    treatment applies to tracking numbers, order numbers, PO numbers, etc.
    """
    if not value:
        return None
    if _appears_in(value, haystack_norm):
        return value
    for part in (p.strip() for p in _MULTI_VALUE_SPLIT_RE.split(value)):
        if part and _appears_in(part, haystack_norm):
            return part
    return None


def _extract_url_tokens(url: str) -> list[str]:
    """Pull the discriminating tokens out of a URL: query-string values and the
    last few path segments. We use these as anchors to test whether the URL is
    grounded in the email body — a hallucinated tracking link will carry a
    tracking-number-looking token that the body doesn't contain."""
    tokens: list[str] = []
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return tokens
    # Query-string values often carry the tracking number / order id.
    for _key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=False).items():
        for v in values:
            if len(v) >= 6:  # ignore "1", "yes", "en_US", etc.
                tokens.append(v)
    # Last 2 path segments — vendors sometimes encode the id in the path.
    for seg in [s for s in parsed.path.split("/") if s][-2:]:
        if len(seg) >= 6:
            tokens.append(seg)
    return tokens


def _ground(parsed: ParsedEmail, body: str, subject: str, sender: str) -> ParsedEmail:
    """Drop any extracted field whose value isn't actually present in the email.

    Vendor-agnostic check: the source text is the email body + subject + sender.
    A field that survived the LLM step but doesn't appear anywhere in the source
    is, by definition, hallucinated — drop it to None and let the downstream
    pipeline decide what to do without that value (e.g. build a canonical
    carrier URL from a verified tracking number).

    Notes on what we deliberately do *not* ground here:
    - vendor / item_description / notes / status_signal: paraphrased/normalized
      from the body by design (a "McMaster-Carr" vendor field is fine even when
      the body only says "McMaster Carr"; the model is also expected to canonicalize
      carrier names like "FedEx" — handled separately below).
    - dates and lead-time: numeric/date fields with their own plausibility gates
      elsewhere in the pipeline.
    """
    haystack = f"{subject}\n{sender}\n{body}"
    norm = _normalize(haystack)

    new_tracking = _first_grounded(parsed.tracking_number, norm) if parsed.tracking_number else None
    if parsed.tracking_number and new_tracking != parsed.tracking_number:
        if new_tracking is None:
            log.warning(
                "grounding: dropping tracking_number %r — not present in email body",
                parsed.tracking_number,
            )
        else:
            log.info(
                "grounding: narrowed tracking_number %r → %r (multi-value)",
                parsed.tracking_number, new_tracking,
            )

    new_carrier = _first_grounded(parsed.carrier, norm) if parsed.carrier else None
    if parsed.carrier and new_carrier != parsed.carrier:
        if new_carrier is None:
            log.warning(
                "grounding: dropping carrier %r — not present in email body",
                parsed.carrier,
            )
        else:
            log.info(
                "grounding: narrowed carrier %r → %r (multi-value)",
                parsed.carrier, new_carrier,
            )

    new_url = parsed.tracking_url
    if new_url:
        # A tracking URL is grounded if any discriminating token from it appears
        # in the body (matches on the tracking number, order id, or path-segment
        # the URL carries). If nothing in the URL is anchored to the body, the
        # whole URL is treated as hallucinated.
        tokens = _extract_url_tokens(new_url)
        if tokens and not any(_appears_in(t, norm) for t in tokens):
            log.warning(
                "grounding: dropping tracking_url %r — no token (%s) appears in body",
                new_url, tokens,
            )
            new_url = None
        # Belt and suspenders: if the URL carries a tracking-number-shaped
        # token that disagrees with the verified tracking_number, drop the URL.
        # Catches the cross-contamination pattern where the model copies a URL
        # from a different shipment's template (e.g. a UPS deep-link with a
        # totally different shipment's 1Z… number).
        elif new_tracking:
            tn_norm = _normalize(new_tracking)
            for tok in tokens:
                t_norm = _normalize(tok)
                # "Tracking-shaped": 10+ chars, contains at least one digit
                # (excludes pure word path-segments like "fedextrack" or
                # "tracking"), and isn't our verified tracking number. If the
                # token also doesn't appear in the body, the URL is anchored
                # to some other shipment.
                if (
                    len(t_norm) >= 10
                    and any(c.isdigit() for c in t_norm)
                    and t_norm != tn_norm
                    and t_norm not in norm
                ):
                    log.warning(
                        "grounding: dropping tracking_url %r — carries "
                        "tracking-shaped token %r that disagrees with "
                        "verified tracking_number %r",
                        new_url, tok, new_tracking,
                    )
                    new_url = None
                    break

    new_order = _first_grounded(parsed.order_number, norm) if parsed.order_number else None
    if parsed.order_number and new_order != parsed.order_number:
        if new_order is None:
            log.warning(
                "grounding: dropping order_number %r — not present in email body",
                parsed.order_number,
            )
        else:
            log.info(
                "grounding: narrowed order_number %r → %r (multi-value)",
                parsed.order_number, new_order,
            )

    new_po = _first_grounded(parsed.po_number, norm) if parsed.po_number else None
    if parsed.po_number and new_po != parsed.po_number:
        if new_po is None:
            log.warning(
                "grounding: dropping po_number %r — not present in email body",
                parsed.po_number,
            )
        else:
            log.info(
                "grounding: narrowed po_number %r → %r (multi-value)",
                parsed.po_number, new_po,
            )

    if (
        new_tracking == parsed.tracking_number
        and new_carrier == parsed.carrier
        and new_url == parsed.tracking_url
        and new_order == parsed.order_number
        and new_po == parsed.po_number
    ):
        return parsed
    return replace(
        parsed,
        tracking_number=new_tracking,
        carrier=new_carrier,
        tracking_url=new_url,
        order_number=new_order,
        po_number=new_po,
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
    # Ground the extracted fields against the actual email text before handing
    # off to the inbox pipeline. The LLM occasionally cross-contaminates fields
    # from similar templates (e.g. fills tracking_url with a UPS deep-link
    # carrying a different shipment's tracking number); the grounding pass
    # drops any field whose value isn't substring-present in the source text.
    parsed = _ground(parsed, body=body, subject=subject, sender=sender)
    log.info("parsed email: %s", asdict(parsed))
    return parsed
