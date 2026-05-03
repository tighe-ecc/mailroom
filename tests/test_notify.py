"""Unit tests for mailroom.notify dispatch.

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
             patch.object(notify.os.path, "isfile", return_value=False), \
             patch.object(notify.subprocess, "run") as mock_run:
            notify.send(**send_kwargs)
            return mock_run

    def test_status_title_and_vendor_subtitle_passed_to_terminal_notifier(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            subtitle="MARK-10 Corporation",
            message="Series R50 torque sensor",
            url="http://localhost:47821",
        )
        args = mock_run.call_args.args[0]
        self.assertEqual(args[args.index("-title") + 1], "Shipped")
        self.assertEqual(args[args.index("-subtitle") + 1], "MARK-10 Corporation")
        self.assertEqual(args[args.index("-message") + 1], "Series R50 torque sensor")

    def test_url_routed_via_execute_with_absolute_open_path(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
            url="http://localhost:47821",
        )
        args = mock_run.call_args.args[0]
        self.assertEqual(
            args[args.index("-execute") + 1],
            "/usr/bin/open http://localhost:47821",
        )
        self.assertNotIn("-open", args)

    def test_execute_shell_quotes_url_with_metacharacters(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
            url="http://localhost:47821/pkg?x=1&y=2",
        )
        args = mock_run.call_args.args[0]
        # `&` must not be interpreted as a shell background operator.
        self.assertEqual(
            args[args.index("-execute") + 1],
            "/usr/bin/open 'http://localhost:47821/pkg?x=1&y=2'",
        )

    def test_app_icon_added_when_static_icon_exists(self):
        self.assertTrue(notify._ICON_PATH.exists(), f"{notify._ICON_PATH} missing")
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
        )
        args = mock_run.call_args.args[0]
        self.assertIn("-appIcon", args)
        self.assertTrue(args[args.index("-appIcon") + 1].endswith("/static/icon.png"))

    def test_subtitle_omitted_when_none(self):
        mock_run = self._run_send(
            tn_path="/opt/homebrew/bin/terminal-notifier",
            osa_path="/usr/bin/osascript",
            title="Shipped",
            subtitle=None,
            message="Widget",
        )
        args = mock_run.call_args.args[0]
        self.assertNotIn("-subtitle", args)

    def test_homebrew_fallback_path_used_when_path_excludes_brew(self):
        # launchd's default PATH excludes /opt/homebrew/bin. shutil.which()
        # returns None but the explicit fallback paths must still locate it.
        def fake_isfile(path: str) -> bool:
            return path == "/opt/homebrew/bin/terminal-notifier"

        def fake_access(path: str, mode: int) -> bool:
            return path == "/opt/homebrew/bin/terminal-notifier"

        with patch.object(notify.shutil, "which", return_value=None), \
             patch.object(notify.os.path, "isfile", side_effect=fake_isfile), \
             patch.object(notify.os, "access", side_effect=fake_access), \
             patch.object(notify.subprocess, "run") as mock_run:
            notify.send(title="Shipped", message="Widget", url="http://localhost:47821")
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/opt/homebrew/bin/terminal-notifier")

    def test_osascript_fallback_when_terminal_notifier_unavailable(self):
        mock_run = self._run_send(
            tn_path=None,
            osa_path="/usr/bin/osascript",
            title="Shipped",
            message="Widget",
            url="http://localhost:47821",
        )
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/osascript")
        # osascript path can't carry a click action; URL must not leak in.
        self.assertNotIn("http://localhost:47821", args)

    def test_silent_when_no_backend_available(self):
        mock_run = self._run_send(
            tn_path=None,
            osa_path=None,
            title="Shipped",
            message="Widget",
            url="http://localhost:47821",
        )
        mock_run.assert_not_called()


class NotifyStatusChangeTests(unittest.TestCase):
    def test_pre_shipment_to_shipped_triggers_shipped(self):
        with patch.object(notify, "send") as mock_send:
            notify.notify_status_change("Widget", "ordered", "pre_transit", None, vendor="V")
        kw = mock_send.call_args.kwargs
        self.assertEqual(kw["title"], "Shipped")
        self.assertEqual(kw["subtitle"], "V")

    def test_status_is_title_and_vendor_is_subtitle(self):
        with patch.object(notify, "send") as mock_send:
            notify.notify_status_change(
                description="Endmills",
                old_status="out_for_delivery",
                new_status="delivered",
                location="San Francisco, CA",
                vendor="Grainger",
            )
        kw = mock_send.call_args.kwargs
        self.assertEqual(kw["title"], "Delivered")
        self.assertEqual(kw["subtitle"], "Grainger")
        self.assertIn("Endmills", kw["message"])
        self.assertIn("San Francisco, CA", kw["message"])

    def test_uninteresting_transition_does_not_notify(self):
        with patch.object(notify, "send") as mock_send:
            notify.notify_status_change("Widget", "in_transit", "in_transit", None)
            mock_send.assert_not_called()

    def test_every_active_transition_passes_dashboard_url(self):
        cases = [
            (None, "pre_transit"),
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
                self.assertEqual(mock_send.call_args.kwargs.get("url"), notify.DASHBOARD_URL)


if __name__ == "__main__":
    unittest.main()
