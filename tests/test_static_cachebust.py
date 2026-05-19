"""The base template's `import` of static/feedback-button.js needs a
cache-busting query string, or browsers cache the kit's pre-update version
and the user has to know to hard-refresh after every `git pull`. The
static_v() helper hashes the file's content so the URL changes IFF the
file does.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class StaticVTests(unittest.TestCase):
    def test_returns_short_hex_string_for_real_file(self) -> None:
        v = app._static_v("feedback-button.js")
        self.assertEqual(len(v), 10)
        self.assertTrue(all(c in "0123456789abcdef" for c in v))

    def test_returns_empty_string_for_missing_file(self) -> None:
        # Graceful fallback so a refactor that renames an asset doesn't 500
        # the page; the URL stays valid (just unfilled), and the cache will
        # eventually expire on its own.
        self.assertEqual(app._static_v("does-not-exist.js"), "")

    def test_hash_changes_when_file_changes(self) -> None:
        # Two different files should hash to different values. Use whatever
        # other file lives in static/ as the comparator.
        v_button = app._static_v("feedback-button.js")
        other = next(
            (f.name for f in (ROOT / "static").iterdir()
             if f.is_file() and f.name != "feedback-button.js"),
            None,
        )
        if other is None:
            self.skipTest("no second static file to compare against")
        v_other = app._static_v(other)
        self.assertNotEqual(v_button, v_other)

    def test_registered_as_template_global(self) -> None:
        # base.html invokes `static_v('feedback-button.js')` — the helper
        # must be exposed in templates.env.globals or the page 500's.
        self.assertIn("static_v", app.templates.env.globals)
        self.assertEqual(
            app.templates.env.globals["static_v"]("feedback-button.js"),
            app._static_v("feedback-button.js"),
        )


if __name__ == "__main__":
    unittest.main()
