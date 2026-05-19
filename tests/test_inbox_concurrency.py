"""Inbox file-lock + vanish-tolerance tests.

Covers the bug where the launchd poll script and the FastAPI watcher both
scan the inbox at once, one wins `shutil.move`, the other raises
FileNotFoundError and inflates the `failed` count even though the file is
safely in `processed/`.

Run: .venv/bin/python -m unittest tests.test_inbox_concurrency -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailroom import inbox, parser


def _eml_text(subject: str = "Test", body: str = "hello") -> bytes:
    return (
        f"From: sender@example.com\r\n"
        f"To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 18 May 2026 12:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


class SafeMoveTests(unittest.TestCase):
    def test_safe_move_tolerates_vanished_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "ghost.eml"
            dst = Path(tmp) / "dst" / "ghost.eml"
            dst.parent.mkdir()
            # No src file. _safe_move should swallow FileNotFoundError.
            try:
                inbox._safe_move(src, dst)
            except Exception as e:
                self.fail(f"_safe_move raised on vanished source: {e!r}")

    def test_safe_move_succeeds_on_real_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "real.eml"
            src.write_bytes(b"hi")
            dst = Path(tmp) / "dst" / "real.eml"
            dst.parent.mkdir()
            inbox._safe_move(src, dst)
            self.assertFalse(src.exists())
            self.assertTrue(dst.exists())


class InboxFileLockTests(unittest.TestCase):
    def test_file_lock_serializes_two_holders(self):
        # Two context-manager acquisitions on the same root: the second blocks
        # until the first releases. We can't easily test "blocks" without
        # threads, so just confirm the lockfile is created and reusable.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with inbox._inbox_file_lock(root):
                lockfile = root / inbox.INTERNAL_SUBDIR / ".process.lock"
                self.assertTrue(lockfile.exists())
            # Second acquisition after release should still succeed.
            with inbox._inbox_file_lock(root):
                pass


class ProcessInboxVanishTolerance(unittest.TestCase):
    """End-to-end: a file that vanishes between snapshot and processing must
    not be counted as `failed`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Override the inbox dir for this test.
        self._old_env = os.environ.get(inbox.INBOX_ROOT_ENV)
        os.environ[inbox.INBOX_ROOT_ENV] = str(self.root)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(inbox.INBOX_ROOT_ENV, None)
        else:
            os.environ[inbox.INBOX_ROOT_ENV] = self._old_env
        self.tmp.cleanup()

    def test_vanished_file_not_counted_as_failed(self):
        # Drop one real .eml plus one that we'll make vanish mid-process.
        good = self.root / "good.eml"
        ghost = self.root / "ghost.eml"
        good.write_bytes(_eml_text())
        ghost.write_bytes(_eml_text(subject="ghost"))

        # Patch parser.parse_email so we don't hit OpenAI. For "good" return an
        # unrecognized result; for "ghost" simulate another process beating us:
        # the file disappears before we get to load it.
        def fake_parse(subject, sender, body):
            return parser.ParsedEmail(
                kind="unknown",
                vendor=None,
                order_number=None,
                po_number=None,
                item_description=None,
                ordered_date=None,
                ordered_date_confidence=0.0,
                promised_ship_date=None,
                promised_delivery_date=None,
                lead_time_days=None,
                tracking_number=None,
                carrier=None,
                tracking_url=None,
                status_signal=None,
                confidence=0.0,
                notes=None,
            )

        orig_load = inbox._load_eml

        def vanishing_load(path):
            if path.name == "ghost.eml":
                # Simulate another process: delete the file mid-flight.
                path.unlink(missing_ok=True)
                # _load_eml opens the file — raise FileNotFoundError to mirror
                # what the real call would now do.
                raise FileNotFoundError(str(path))
            return orig_load(path)

        with mock.patch.object(parser, "parse_email", side_effect=fake_parse), \
             mock.patch.object(inbox, "_load_eml", side_effect=vanishing_load):
            summary = inbox.process_inbox()

        # The ghost file vanished before we could load it. It should NOT count
        # as `failed` (that would inflate the alert metric). It should also
        # decrement `seen` so the user-visible counts match reality.
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["seen"], 1)
        self.assertEqual(summary["unrecognized"], 1)


if __name__ == "__main__":
    unittest.main()
