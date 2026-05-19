"""Tests for the notify dedup + quiet-mode gate.

Context: today the re-parse sub-agent's tests ran live LLM scans over the
user's real ~/Mailroom archive during setUp, and each successful re-parse
fired a real macOS "Shipped" notification through terminal-notifier. Two
guardrails landed in response:

  1. MAILROOM_QUIET=1 short-circuits notify.send() entirely, so a test that
     drives _apply doesn't reach the user's Notification Center.
  2. notify_status_change forwards a stable per-row group_id to send(),
     which translates into terminal-notifier's ``-group`` flag — a row
     that updates N times collapses to one Notification Center entry
     instead of stacking N duplicates.

This file tests the wiring, not the actual notification display.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mailroom import notify


class QuietGateTests(unittest.TestCase):
    def test_send_is_no_op_when_quiet_is_set(self) -> None:
        with patch.dict(os.environ, {"MAILROOM_QUIET": "1"}, clear=False), \
             patch.object(notify.subprocess, "run") as run_mock, \
             patch.object(notify, "_find_terminal_notifier",
                          return_value="/opt/homebrew/bin/terminal-notifier"):
            notify.send(title="Shipped", message="x")
        run_mock.assert_not_called()

    def test_send_runs_terminal_notifier_when_not_quiet(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MAILROOM_QUIET", None)
            with patch.object(notify.subprocess, "run") as run_mock, \
                 patch.object(notify, "_find_terminal_notifier",
                              return_value="/opt/homebrew/bin/terminal-notifier"):
                notify.send(title="Shipped", message="x")
            run_mock.assert_called_once()


class GroupIdTests(unittest.TestCase):
    def test_send_passes_group_flag_when_group_id_given(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MAILROOM_QUIET", None)
            with patch.object(notify.subprocess, "run") as run_mock, \
                 patch.object(notify, "_find_terminal_notifier",
                              return_value="/opt/homebrew/bin/terminal-notifier"):
                notify.send(title="Shipped", message="x", group_id="mailroom:19")
            args, _ = run_mock.call_args
            argv = args[0]
            self.assertIn("-group", argv)
            self.assertEqual(argv[argv.index("-group") + 1], "mailroom:19")

    def test_send_omits_group_flag_when_no_group_id(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MAILROOM_QUIET", None)
            with patch.object(notify.subprocess, "run") as run_mock, \
                 patch.object(notify, "_find_terminal_notifier",
                              return_value="/opt/homebrew/bin/terminal-notifier"):
                notify.send(title="Shipped", message="x")
            args, _ = run_mock.call_args
            self.assertNotIn("-group", args[0])

    def test_notify_status_change_forwards_row_id_as_group_id(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MAILROOM_QUIET", None)
            with patch.object(notify, "send") as send_mock:
                notify.notify_status_change(
                    description="Parts",
                    old_status="ordered",
                    new_status="pre_transit",
                    location=None,
                    vendor="Acme",
                    row_id=42,
                )
            kwargs = send_mock.call_args.kwargs
            self.assertEqual(kwargs["group_id"], "mailroom:42")

    def test_notify_status_change_no_group_id_when_row_id_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MAILROOM_QUIET", None)
            with patch.object(notify, "send") as send_mock:
                notify.notify_status_change(
                    description="Parts",
                    old_status="ordered",
                    new_status="pre_transit",
                    location=None,
                    vendor="Acme",
                )
            kwargs = send_mock.call_args.kwargs
            self.assertIsNone(kwargs.get("group_id"))


if __name__ == "__main__":
    unittest.main()
