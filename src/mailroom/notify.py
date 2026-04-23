"""macOS desktop notifications via osascript.

Prefers terminal-notifier if installed (better UX, stays in Notification Center,
supports click-to-open URL); falls back to osascript (always available on macOS,
but no click action).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess


APP_TITLE = "Mailroom"
DASHBOARD_URL = os.environ.get("MAILROOM_URL", "http://localhost:8501")

PRE_SHIPMENT = {"ordered", "confirmed", "in_fulfillment", None}
SHIPPED_FAMILY = {"pre_transit", "in_transit", "out_for_delivery"}


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, message: str, url: str | None = None) -> None:
    """Fire a macOS notification. Silent on non-macOS systems.

    If ``url`` is provided and terminal-notifier is installed, clicking the
    notification opens that URL in the default browser. The osascript fallback
    cannot bind a click action, so the URL is ignored there.
    """
    tn = shutil.which("terminal-notifier")
    if tn:
        args = [tn, "-title", APP_TITLE, "-subtitle", title, "-message", message, "-sound", "default"]
        if url:
            # -execute runs through a shell; -open is unreliable on recent macOS.
            args += ["-execute", f"open {shlex.quote(url)}"]
        subprocess.run(args, check=False)
        return

    osa = shutil.which("osascript")
    if not osa:
        return
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "{_escape_applescript(APP_TITLE)}" '
        f'subtitle "{_escape_applescript(title)}" '
        f'sound name "default"'
    )
    subprocess.run([osa, "-e", script], check=False)


def notify_status_change(
    description: str, old_status: str | None, new_status: str, location: str | None
) -> None:
    """Fire a notification for notable status transitions. No-op if transition is uninteresting."""
    desc = description or "Package"
    where = f" ({location})" if location else ""

    # pre-shipment → shipped (first time a tracking number appears)
    if old_status in PRE_SHIPMENT and new_status in SHIPPED_FAMILY:
        send("Shipped", desc, url=DASHBOARD_URL)
        return

    if new_status == "out_for_delivery" and old_status != "out_for_delivery":
        send("Arriving today", f"{desc}{where}", url=DASHBOARD_URL)
    elif new_status == "delivered" and old_status != "delivered":
        send("Delivered", f"{desc}{where}", url=DASHBOARD_URL)
    elif new_status in {"return_to_sender", "failure", "error"} and old_status != new_status:
        send("Delivery issue", f"{desc}: {new_status.replace('_', ' ')}", url=DASHBOARD_URL)
    elif new_status == "confirmed" and old_status == "ordered":
        send("Order confirmed", desc, url=DASHBOARD_URL)
