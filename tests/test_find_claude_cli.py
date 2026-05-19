"""The Claude Code CLI typically lives at ~/.local/bin/claude, but the
launchd-spawned FastAPI process gets a minimal PATH that doesn't include
~/.local/bin. shutil.which alone returns None and the Expedite trigger
silently no-ops. These tests pin the multi-strategy discovery so it keeps
working across (a) the operator escape-hatch env var, (b) anything actually
on PATH, and (c) well-known per-user / system install locations.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class FindClaudeCliTests(unittest.TestCase):
    def test_env_var_override_wins_when_executable(self) -> None:
        with patch.dict(os.environ, {"MAILROOM_CLAUDE_BIN": "/bin/sh"}, clear=False), \
             patch.object(app.shutil, "which", return_value="/should/not/be/used"):
            self.assertEqual(app._find_claude_cli(), "/bin/sh")

    def test_env_var_ignored_if_not_executable(self) -> None:
        # If the operator sets the var to a bogus path, we fall back to PATH
        # rather than silently using the bad value.
        with patch.dict(os.environ, {"MAILROOM_CLAUDE_BIN": "/does/not/exist"}, clear=False), \
             patch.object(app.shutil, "which", return_value="/usr/local/bin/claude"):
            self.assertEqual(app._find_claude_cli(), "/usr/local/bin/claude")

    def test_falls_back_to_shutil_which(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(app.shutil, "which", return_value="/opt/homebrew/bin/claude"):
            self.assertEqual(app._find_claude_cli(), "/opt/homebrew/bin/claude")

    def test_falls_back_to_known_path_when_path_lookup_fails(self) -> None:
        # The bug we're fixing: shutil.which returns None because PATH is
        # narrow, but the binary really is sitting at ~/.local/bin/claude.
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(app.shutil, "which", return_value=None), \
             patch.object(app.Path, "home", return_value=Path("/tmp/_fake_home")):
            fake = Path("/tmp/_fake_home/.local/bin/claude")
            fake.parent.mkdir(parents=True, exist_ok=True)
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            try:
                self.assertEqual(app._find_claude_cli(), str(fake))
            finally:
                fake.unlink(missing_ok=True)

    def test_returns_none_when_nothing_found(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(app.shutil, "which", return_value=None), \
             patch.object(app.Path, "home", return_value=Path("/tmp/_fake_home_empty")):
            # /tmp/_fake_home_empty/.local/bin/claude does not exist
            self.assertIsNone(app._find_claude_cli())


if __name__ == "__main__":
    unittest.main()
