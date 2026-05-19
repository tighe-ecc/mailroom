"""Unit tests for the local-agent expedite trigger in app.py.

Phase 2 of the feedback-kit Expedite hook runs a headless ``claude`` CLI
subprocess locally instead of POSTing to a remote claude.ai routine. The
mailroom user's feedback often cites parsed-email files under
``~/Mailroom/.mailroom/processed/*.eml`` that a remote routine can't see, so
the agent has to run on this machine with filesystem access to the repo.

These tests pin the contract of ``app._expedite_local()``:
  - graceful no-op when ``claude`` is not on PATH
  - lockfile semantics: skip when another drain is live, reclaim when stale
  - subprocess.Popen is invoked with the expected args (mocked — we never
    actually spawn ``claude`` in CI)
  - the prompt file exists at the repo root and is non-empty

We mock ``shutil.which`` and ``subprocess.Popen`` so no real process runs.
The lockfile path is monkeypatched onto a tempdir so the user's real
``~/Mailroom/.mailroom/.feedback-agent.pid`` is never touched.

Run: .venv/bin/python -m unittest tests.test_feedback_expedite -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the repo root importable (matches how the other tests reach app.py).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class ExpediteLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        # Redirect the lockfile and log to a tempdir so tests never touch
        # the user's real ~/Mailroom directory.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.lockfile = self.tmpdir / ".feedback-agent.pid"
        self.logfile = self.tmpdir / "agent.log"
        self._lock_patch = patch.object(app, "_EXPEDITE_LOCKFILE", self.lockfile)
        self._log_patch = patch.object(app, "_EXPEDITE_LOG_FILE", self.logfile)
        self._lock_patch.start()
        self._log_patch.start()
        self.addCleanup(self._lock_patch.stop)
        self.addCleanup(self._log_patch.stop)

    # ---- claude not on PATH ------------------------------------------------

    def test_claude_missing_logs_warning_and_returns(self) -> None:
        """If ``claude`` isn't installed, we log a warning and bail without
        touching the lockfile or attempting to spawn anything. This is the
        common state on a freshly-cloned dev machine and must not break the
        /feedback endpoint."""
        with patch.object(app.shutil, "which", return_value=None), \
             patch.object(app.subprocess, "Popen") as popen, \
             self.assertLogs(level="WARNING") as captured:
            app._expedite_local()
        popen.assert_not_called()
        self.assertFalse(self.lockfile.exists())
        self.assertTrue(
            any("claude" in m.lower() and "path" in m.lower() for m in captured.output),
            f"expected a PATH warning, got: {captured.output}",
        )

    # ---- lockfile held by a live process -----------------------------------

    def test_running_drain_blocks_second_spawn(self) -> None:
        """If the lockfile points at a PID that's still alive, we treat the
        drain as in-flight and skip — no second ``claude`` spawn, no
        clobbering the lockfile."""
        # Our own PID is guaranteed to be alive.
        self.lockfile.write_text(str(os.getpid()), encoding="utf-8")
        with patch.object(app.shutil, "which", return_value="/usr/local/bin/claude"), \
             patch.object(app.subprocess, "Popen") as popen, \
             self.assertLogs(level="INFO") as captured:
            app._expedite_local()
        popen.assert_not_called()
        # Lockfile is untouched.
        self.assertEqual(self.lockfile.read_text(encoding="utf-8"), str(os.getpid()))
        self.assertTrue(
            any("already running" in m.lower() for m in captured.output),
            f"expected an already-running info log, got: {captured.output}",
        )

    # ---- stale lockfile gets reclaimed -------------------------------------

    def test_stale_lockfile_is_reclaimed_and_spawn_proceeds(self) -> None:
        """A lockfile from a previous run whose PID is gone must be cleared,
        then a fresh spawn proceeds. Otherwise a crashed agent would block
        every future Expedite click forever."""
        # Pick a PID that is extremely unlikely to be alive. We can't pick
        # PID 0 (kill -0 0 has special meaning), so use a large value and
        # patch the liveness probe to confirm it as dead.
        self.lockfile.write_text("999999", encoding="utf-8")
        fake_proc = MagicMock(pid=4242)
        with patch.object(app.shutil, "which", return_value="/usr/local/bin/claude"), \
             patch.object(app, "_expedite_pid_is_alive", return_value=False), \
             patch.object(app.subprocess, "Popen", return_value=fake_proc) as popen:
            app._expedite_local()
        popen.assert_called_once()
        # Lockfile now holds the new child PID.
        self.assertEqual(self.lockfile.read_text(encoding="utf-8"), "4242")

    # ---- happy path --------------------------------------------------------

    def test_happy_path_invokes_popen_with_expected_args(self) -> None:
        """No lockfile, claude on PATH, prompt readable → Popen is called
        with --print --add-dir <repo> <prompt>, detached via
        start_new_session, stdin closed."""
        fake_proc = MagicMock(pid=12345)
        with patch.object(app.shutil, "which", return_value="/usr/local/bin/claude"), \
             patch.object(app.subprocess, "Popen", return_value=fake_proc) as popen:
            app._expedite_local()

        popen.assert_called_once()
        args, kwargs = popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "/usr/local/bin/claude")
        self.assertIn("--print", cmd)
        self.assertIn("--add-dir", cmd)
        # --add-dir's value is the repo root.
        idx = cmd.index("--add-dir")
        self.assertEqual(cmd[idx + 1], str(app.ROOT))
        # The prompt is the final positional argument and matches the file
        # contents — readable, non-empty.
        prompt_arg = cmd[-1]
        self.assertEqual(
            prompt_arg,
            app._EXPEDITE_PROMPT_FILE.read_text(encoding="utf-8"),
        )
        self.assertTrue(prompt_arg.strip(), "prompt argument was empty")
        # Detached: own session, stdin disconnected.
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertEqual(kwargs.get("stdin"), app.subprocess.DEVNULL)
        self.assertEqual(kwargs.get("cwd"), str(app.ROOT))
        # Lockfile holds the child PID for the next caller's liveness probe.
        self.assertEqual(self.lockfile.read_text(encoding="utf-8"), "12345")

    # ---- prompt file shape -------------------------------------------------

    def test_prompt_file_exists_and_is_non_empty(self) -> None:
        """The prompt file is what gives the headless agent its marching
        orders. If it's missing or empty, the agent has no brief and we'd
        rather not spawn at all."""
        path = app._EXPEDITE_PROMPT_FILE
        self.assertTrue(path.exists(), f"missing prompt file: {path}")
        content = path.read_text(encoding="utf-8")
        self.assertTrue(content.strip(), "prompt file is empty")
        # Sanity: the prompt mentions the load-bearing rules so we don't
        # regress to an empty stub.
        self.assertIn("feedback.md", content)


if __name__ == "__main__":
    unittest.main()
