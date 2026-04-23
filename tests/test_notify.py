"""Unit tests for mailroom.notify click-to-open behavior.

Run: .venv/bin/python -m unittest tests.test_notify -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mailroom import notify


class SendTests(unittest.TestCase):
    def _run_send(self, *, tn_path: str | None, osa_path: str | None, **send_kwargs):
        def fake_which(cmd: str) -> str | None:
            if cmd == "terminal-notifier":
                return tn_path
            if cmd == "osascript":
                return osa_path
            return None

        with patch.object(notify.shutil, "which", side_effect=fake_which), \
             patch.object(notify.subprocess, "run") as mock_run:
            notify.send(**send_kwargs)
            return mock_run

    def test_terminal_notifier_with_url_includes_open_flag(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
            url="http://localhost:8501",
        )
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/opt/homebrew/bin/terminal-notifier")
        self.assertIn("-open", args)
        self.assertEqual(args[args.index("-open") + 1], "http://localhost:8501")
        self.assertIn("-title", args)
        self.assertEqual(args[args.index("-title") + 1], notify.APP_TITLE)

    def test_terminal_notifier_without_url_omits_open_flag(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
        )
        args = mock_run.call_args.args[0]
        self.assertNotIn("-open", args)

    def test_osascript_fallback_ignores_url(self):
        mock_run = self._run_send(
            tn_path=None,
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
            url="http://localhost:8501",
        )
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/osascript")
        # osascript path has no "-open" concept; URL must not leak in.
        self.assertNotIn("http://localhost:8501", args)

    def test_silent_when_no_backend_available(self):
        mock_run = self._run_send(
            tn_path=None,
            osa_path=None,
            title="Shipped",
            message="Widget",
            url="http://localhost:8501",
        )
        mock_run.assert_not_called()


class NotifyStatusChangeTests(unittest.TestCase):
    def test_every_transition_passes_dashboard_url(self):
        cases = [
            (None, "pre_transit"),          # shipped
            ("in_transit", "out_for_delivery"),
            ("out_for_delivery", "delivered"),
            ("in_transit", "return_to_sender"),
            ("ordered", "confirmed"),
        ]
        for old, new in cases:
            with self.subTest(old=old, new=new), \
                 patch.object(notify, "send") as mock_send:
                notify.notify_status_change("Widget", old, new, location=None)
                mock_send.assert_called_once()
                kwargs = mock_send.call_args.kwargs
                self.assertEqual(kwargs.get("url"), notify.DASHBOARD_URL)

    def test_uninteresting_transition_does_not_notify(self):
        with patch.object(notify, "send") as mock_send:
            notify.notify_status_change("Widget", "in_transit", "in_transit", location=None)
            mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
