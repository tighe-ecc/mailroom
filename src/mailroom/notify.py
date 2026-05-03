"""macOS desktop notifications via terminal-notifier (with osascript fallback).

Notifications are fired headlessly from whatever process triggered the status
change (the GUI server's watcher or the launchd-spawned poller). Clicking a
notification runs ``/usr/bin/open <dashboard-url>`` via terminal-notifier's
``-execute`` hook, which opens the dashboard in the user's default browser.

Notification source is the Terminal app (terminal-notifier owns the bundle).
We don't try to override via ``-sender``: pointing it at a separate Mailroom.app
bundle made notifications appear under a Mailroom header but silently dropped
both ``-execute`` and ``-activate`` click handlers on macOS Tahoe, leaving the
notification non-clickable. terminal-notifier owning the click is the trade we
make for reliable behavior.

Notification structure:
    title    = status label, e.g. "Shipped" / "Delivered" / "Out for delivery"
    subtitle = vendor, e.g. "MARK-10 Corporation"
    message  = item description, optionally with last-event location
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


DASHBOARD_URL = os.environ.get("MAILROOM_URL", "http://localhost:47821")

# Mailroom icon at <repo>/static/icon.png — used as terminal-notifier's
# -appIcon (notification body image).
_ICON_PATH = Path(__file__).resolve().parents[2] / "static" / "icon.png"

# Under launchd the inherited PATH typically excludes Homebrew. Hard-coded
# fallbacks let the poller subprocess find terminal-notifier even when
# `shutil.which` comes up empty.
_TN_FALLBACK_PATHS = (
    "/opt/homebrew/bin/terminal-notifier",
    "/usr/local/bin/terminal-notifier",
)

PRE_SHIPMENT = {"ordered", "confirmed", "in_fulfillment", None}
SHIPPED_FAMILY = {"pre_transit", "in_transit", "out_for_delivery"}


def _find_terminal_notifier() -> str | None:
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for path in _TN_FALLBACK_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send(
    *,
    title: str,
    subtitle: str | None = None,
    message: str,
    url: str | None = None,
) -> None:
    """Fire a macOS notification. Silent on non-macOS systems."""
    tn = _find_terminal_notifier()
    if tn:
        args = [tn, "-title", title, "-message", message, "-sound", "default"]
        if subtitle:
            args += ["-subtitle", subtitle]
        if _ICON_PATH.exists():
            args += ["-appIcon", _ICON_PATH.as_uri()]
        if url:
            # -execute runs through a shell on click. We use /usr/bin/open
            # rather than `open` (relative) so the click handler isn't
            # subject to PATH at click time — terminal-notifier's click
            # callback can run with a minimal environment.
            args += ["-execute", f"/usr/bin/open {shlex.quote(url)}"]
        subprocess.run(args, check=False)
        return

    osa = shutil.which("osascript")
    if not osa:
        return
    parts = [
        f'display notification "{_escape_applescript(message)}"',
        f'with title "{_escape_applescript(title)}"',
    ]
    if subtitle:
        parts.append(f'subtitle "{_escape_applescript(subtitle)}"')
    parts.append('sound name "default"')
    subprocess.run([osa, "-e", " ".join(parts)], check=False)


_STATUS_TITLES = {
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "return_to_sender": "Return to sender",
    "failure": "Delivery failure",
    "error": "Delivery error",
    "confirmed": "Order confirmed",
}


def _status_title(old: str | None, new: str) -> str | None:
    """Return the notification title for a transition, or None to skip."""
    if old in PRE_SHIPMENT and new in SHIPPED_FAMILY:
        return "Shipped"
    if old == new:
        return None
    if new == "confirmed" and old != "ordered":
        return None
    return _STATUS_TITLES.get(new)


def notify_status_change(
    description: str,
    old_status: str | None,
    new_status: str,
    location: str | None,
    vendor: str | None = None,
) -> None:
    """Fire a notification for notable status transitions. No-op if uninteresting."""
    title = _status_title(old_status, new_status)
    if title is None:
        return

    desc = description or "Package"
    if location:
        desc = f"{desc} ({location})"

    send(title=title, subtitle=vendor or None, message=desc, url=DASHBOARD_URL)
