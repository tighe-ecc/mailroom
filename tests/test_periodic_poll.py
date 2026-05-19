"""Unit tests for the in-process periodic poll loop.

Background: launchd's StartInterval on com.tighe.mailroom.poll is best-effort
and silently drops fires across sleep / App Nap / Low Power Mode. The GUI
process now drives its own poll loop in the FastAPI lifespan as the primary
driver, with the launchd agent kept as a backup. Both share
poll._interval_elapsed() so they coexist without double-polling.

These tests pin the Python side of that wiring:
  - the loop calls poll.poll_once() on each tick
  - exceptions in poll_once() don't kill the task
  - cancellation during shutdown is honored
  - the startup delay is observed before the first fire
  - the tick interval comes from settings.daemon_tick_seconds()

asyncio.sleep is mocked so tests don't actually wait 30 minutes.

Run: .venv/bin/python -m unittest tests.test_periodic_poll -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _StopLoop(Exception):
    """Sentinel raised from a patched asyncio.sleep to break the infinite loop
    after the assertions of interest have run. Tests catch this explicitly so
    a real bug raising the same path would still surface."""


def _make_sleep_stub(fire_count: int, calls: list[float]):
    """Return an async function that records every asyncio.sleep duration and
    raises _StopLoop after ``fire_count`` ticks (each tick = startup sleep +
    inter-tick sleep, so we cap on the post-tick sleep)."""

    async def _sleep(seconds: float) -> None:
        calls.append(float(seconds))
        # Stop after we've recorded fire_count inter-tick sleeps. The first
        # call is the startup delay, then each loop iteration adds one more.
        # fire_count=1 → startup + 1 inter-tick = 2 total calls.
        if len(calls) >= fire_count + 1:
            raise _StopLoop

    return _sleep


class PeriodicPollLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        os.environ["MAILROOM_DB"] = self._tmp.name

        # Stub watcher so importing app.py doesn't start a real fs observer.
        self._watcher_start = patch("app.watcher.start", return_value=None)
        self._watcher_stop = patch("app.watcher.stop", return_value=None)
        self._watcher_start.start()
        self._watcher_stop.start()

        import app as app_module
        self.app_module = app_module

    def tearDown(self) -> None:
        self._watcher_start.stop()
        self._watcher_stop.stop()
        Path(self._tmp.name).unlink(missing_ok=True)
        # The shared settings.json lives next to the temp DB.
        from mailroom import settings
        settings.settings_path().unlink(missing_ok=True)
        os.environ.pop("MAILROOM_DB", None)

    async def test_loop_calls_poll_once_each_tick(self):
        """The loop must drive poll.poll_once() on every wake."""
        calls: list[float] = []
        sleep_stub = _make_sleep_stub(fire_count=3, calls=calls)

        with patch("app.asyncio.sleep", new=sleep_stub), \
             patch("app.poll.poll_once") as mock_poll:
            mock_poll.return_value = {"checked": 0}
            with self.assertRaises(_StopLoop):
                await self.app_module._periodic_poll_loop()

        # 3 fires expected.
        self.assertEqual(mock_poll.call_count, 3)

    async def test_loop_observes_startup_delay_before_first_fire(self):
        """First asyncio.sleep should be the startup delay, not the tick."""
        calls: list[float] = []
        sleep_stub = _make_sleep_stub(fire_count=1, calls=calls)

        with patch("app.asyncio.sleep", new=sleep_stub), \
             patch("app.poll.poll_once", return_value={"checked": 0}):
            with self.assertRaises(_StopLoop):
                await self.app_module._periodic_poll_loop()

        # First sleep = startup delay constant. The actual value isn't load-
        # bearing, but it must NOT be the daemon-tick interval (otherwise the
        # first poll would be delayed by 30 minutes after every GUI restart).
        self.assertEqual(calls[0], self.app_module._POLL_TASK_STARTUP_DELAY_SECONDS)
        # And the startup delay must be much shorter than a daemon tick — if
        # someone "tunes" it up to the full interval, restarts effectively
        # disable polling for 30 minutes.
        self.assertLess(calls[0], 60)

    async def test_loop_uses_daemon_tick_seconds_for_interval(self):
        """Inter-tick sleep must come from settings.daemon_tick_seconds()."""
        calls: list[float] = []
        sleep_stub = _make_sleep_stub(fire_count=1, calls=calls)

        with patch("app.asyncio.sleep", new=sleep_stub), \
             patch("app.poll.poll_once", return_value={"checked": 0}), \
             patch("app.mr_settings.daemon_tick_seconds", return_value=1234):
            with self.assertRaises(_StopLoop):
                await self.app_module._periodic_poll_loop()

        # Second call (after the startup delay + one poll fire) is the tick.
        self.assertEqual(calls[1], 1234)

    async def test_loop_recovers_from_poll_exception(self):
        """A raise inside poll_once must not kill the loop — next tick still fires."""
        calls: list[float] = []
        sleep_stub = _make_sleep_stub(fire_count=3, calls=calls)

        # First call raises, subsequent calls return normally.
        side_effects = [RuntimeError("simulated poll failure"),
                        {"checked": 1},
                        {"checked": 2}]

        with patch("app.asyncio.sleep", new=sleep_stub), \
             patch("app.poll.poll_once", side_effect=side_effects) as mock_poll:
            with self.assertRaises(_StopLoop):
                await self.app_module._periodic_poll_loop()

        # All three ticks fired even though the first one raised.
        self.assertEqual(mock_poll.call_count, 3)

    async def test_loop_honors_cancellation(self):
        """Cancelling the task during a sleep should propagate CancelledError
        out of the loop (lifespan teardown awaits the task)."""
        # Don't patch asyncio.sleep here — patching it globally would also stub
        # the asyncio.sleep(0) calls below, which we need to yield to the loop.
        # Instead, pump the startup-delay constant up to something we won't
        # reach within the test, so the task lands in its first real sleep,
        # then cancel it.
        with patch.object(
                self.app_module, "_POLL_TASK_STARTUP_DELAY_SECONDS", 3600), \
             patch("app.poll.poll_once", return_value={"checked": 0}):
            task = asyncio.create_task(self.app_module._periodic_poll_loop())
            # Yield enough times for the task to reach its first await.
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
