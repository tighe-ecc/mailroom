"""Unit tests for mailroom.settings + the poll-throttle gate.

The "background updates not visible" feedback wanted two things:
  - the dashboard shows whether the poller is alive (last_poll_at)
  - the update frequency is a tunable preference (poll_interval_seconds)

Both are backed by a JSON file at ~/Mailroom/.mailroom/settings.json so the
launchd-spawned poller and the FastAPI process can share the state without
schema churn.

Run: .venv/bin/python -m unittest tests.test_settings -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name

        from mailroom import settings

        self.settings = settings

    def tearDown(self) -> None:
        Path(self._tmp.name).unlink(missing_ok=True)
        sp = self.settings.settings_path()
        sp.unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)

    def test_defaults_when_file_missing(self):
        self.assertEqual(
            self.settings.get_poll_interval_seconds(),
            self.settings.DEFAULT_POLL_INTERVAL_SECONDS,
        )
        self.assertIsNone(self.settings.get_last_poll_at())

    def test_set_poll_interval_round_trips(self):
        self.settings.set_poll_interval_seconds(600)
        self.assertEqual(self.settings.get_poll_interval_seconds(), 600)

    def test_set_poll_interval_clamps_low(self):
        # Anything below MIN gets clamped — 0 or negative would disable polling.
        written = self.settings.set_poll_interval_seconds(5)
        self.assertEqual(written, self.settings.MIN_POLL_INTERVAL_SECONDS)
        self.assertEqual(
            self.settings.get_poll_interval_seconds(),
            self.settings.MIN_POLL_INTERVAL_SECONDS,
        )

    def test_set_poll_interval_clamps_high(self):
        written = self.settings.set_poll_interval_seconds(10**9)
        self.assertEqual(written, self.settings.MAX_POLL_INTERVAL_SECONDS)

    def test_record_poll_at_round_trips(self):
        stamped = self.settings.record_poll_at("2026-05-12T18:00:00+00:00")
        self.assertEqual(stamped, "2026-05-12T18:00:00+00:00")
        self.assertEqual(
            self.settings.get_last_poll_at(), "2026-05-12T18:00:00+00:00"
        )

    def test_corrupt_settings_file_falls_back_to_defaults(self):
        # If something writes garbage to settings.json (manual edit, partial
        # write that escapes the rename), we must not crash the poller —
        # fall back to the default interval and re-stamp from scratch.
        self.settings.settings_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(
            self.settings.get_poll_interval_seconds(),
            self.settings.DEFAULT_POLL_INTERVAL_SECONDS,
        )
        self.assertIsNone(self.settings.get_last_poll_at())


class PollIntervalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name

    def tearDown(self) -> None:
        from mailroom import settings

        Path(self._tmp.name).unlink(missing_ok=True)
        settings.settings_path().unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)

    def test_elapsed_true_when_no_prior_poll(self):
        from mailroom import poll

        self.assertTrue(poll._interval_elapsed())

    def test_elapsed_false_when_within_interval(self):
        from mailroom import poll, settings

        settings.set_poll_interval_seconds(600)
        recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        settings.record_poll_at(recent)
        self.assertFalse(poll._interval_elapsed())

    def test_elapsed_true_when_past_interval(self):
        from mailroom import poll, settings

        settings.set_poll_interval_seconds(600)
        long_ago = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        settings.record_poll_at(long_ago)
        self.assertTrue(poll._interval_elapsed())

    def test_poll_once_skips_carrier_when_within_interval(self):
        """The interval gate must not block the inbox ingest — emails should
        still be processed even when the carrier-poll step is throttled."""
        from mailroom import poll, settings

        settings.set_poll_interval_seconds(600)
        recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        settings.record_poll_at(recent)

        with patch("mailroom.poll.inbox.process_inbox") as mock_inbox:
            mock_inbox.return_value = {
                "seen": 3, "created": 1, "updated": 2,
                "unrecognized": 0, "failed": 0,
            }
            summary = poll.poll_once()

        mock_inbox.assert_called_once()
        self.assertTrue(summary.get("skipped"))
        self.assertEqual(summary["checked"], 0)
        # Inbox numbers still flow through.
        self.assertEqual(summary["inbox_seen"], 3)
        self.assertEqual(summary["inbox_created"], 1)


class PollStatusStalenessTests(unittest.TestCase):
    """Unit tests for the _poll_status() stale-flag logic in app.py.

    Staleness is gated by the *effective* polling period, which is the
    max of the user's configured min-gap and the launchd daemon tick.
    Tests pin MAILROOM_POLL_TICK so the daemon-tick contribution is
    deterministic regardless of the local environment.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name
        # Pin the daemon tick to 60s so user interval dominates in these tests.
        os.environ["MAILROOM_POLL_TICK"] = "60"

    def tearDown(self) -> None:
        from mailroom import settings

        Path(self._tmp.name).unlink(missing_ok=True)
        settings.settings_path().unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)
        os.environ.pop("MAILROOM_POLL_TICK", None)

    def _poll_status(self):
        # Import lazily so MAILROOM_DB env var is set first.
        import importlib
        import app as mailroom_app
        importlib.reload(mailroom_app)
        return mailroom_app._poll_status()

    def test_stale_when_no_poll_recorded(self):
        ps = self._poll_status()
        self.assertTrue(ps["stale"])
        self.assertIsNone(ps["last_poll_local"])

    def test_not_stale_within_interval(self):
        from mailroom import settings
        settings.set_poll_interval_seconds(600)
        recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        settings.record_poll_at(recent)
        ps = self._poll_status()
        self.assertFalse(ps["stale"])
        # The absolute clock time of the last poll should be rendered.
        self.assertIsNotNone(ps["last_poll_local"])

    def test_stale_once_interval_exceeded(self):
        from mailroom import settings
        settings.set_poll_interval_seconds(600)
        # 600 s interval + 60 s grace = stale at 661 s (daemon tick pinned to 60).
        old = (datetime.now(timezone.utc) - timedelta(seconds=661)).isoformat()
        settings.record_poll_at(old)
        ps = self._poll_status()
        self.assertTrue(ps["stale"])

    def test_not_stale_inside_grace_period(self):
        from mailroom import settings
        settings.set_poll_interval_seconds(600)
        # 600 s interval exceeded but still within 60 s grace window
        edge = (datetime.now(timezone.utc) - timedelta(seconds=630)).isoformat()
        settings.record_poll_at(edge)
        ps = self._poll_status()
        self.assertFalse(ps["stale"])

    def test_daemon_tick_dominates_when_larger_than_user_interval(self):
        """User picked 10m, but launchd only fires every 30m. The chip
        must stay green until 30m + 60s grace has passed, otherwise it
        prematurely cries wolf about a working background updater."""
        from mailroom import settings
        os.environ["MAILROOM_POLL_TICK"] = "1800"  # 30 minutes
        settings.set_poll_interval_seconds(600)    # user picked 10 minutes
        # 15 minutes ago — past the user's 10m min, but well inside the 30m daemon tick.
        recent = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        settings.record_poll_at(recent)
        ps = self._poll_status()
        self.assertFalse(ps["stale"])
        self.assertEqual(ps["effective_period_seconds"], 1800)

    def test_user_interval_dominates_when_larger_than_daemon_tick(self):
        """User picked 60m, daemon fires every 30m. Polls only happen
        every 60m, so the chip should respect that."""
        from mailroom import settings
        os.environ["MAILROOM_POLL_TICK"] = "1800"  # 30 minutes
        settings.set_poll_interval_seconds(3600)   # user picked 60 minutes
        # 45 minutes ago — past the daemon tick, but still inside the user's min-gap.
        recent = (datetime.now(timezone.utc) - timedelta(seconds=2700)).isoformat()
        settings.record_poll_at(recent)
        ps = self._poll_status()
        self.assertFalse(ps["stale"])
        self.assertEqual(ps["effective_period_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
