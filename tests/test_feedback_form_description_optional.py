"""Pin the feedback contract: description is optional.

The feedback library originally raised ``ValueError("description is
required")`` and the form's textarea carried the ``required`` attribute.
User-filed feedback asked for description to be optional so the form stays
as low-friction as possible.

This test guards three things:
  - ``feedback.note("")`` writes a row instead of raising
  - the form's textarea no longer has ``required``
  - the client-side guard does not block on empty description alone

Run: .venv/bin/python -m unittest tests.test_feedback_form_description_optional -v
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import feedback  # noqa: E402

FORM_JS = ROOT / "static" / "feedback-button.js"


class DescriptionOptionalLibraryTests(unittest.TestCase):
    def test_note_with_empty_description_writes_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = feedback.note(
                "",
                type="feature",
                title="just a title, no body",
                path=Path(d),
            )
            self.assertTrue(target.exists())
            text = target.read_text(encoding="utf-8")
            self.assertIn("just a title, no body", text)
            self.assertIn("- [ ]", text)

    def test_note_with_whitespace_only_description_writes_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = feedback.note(
                "   \n\t  ",
                type="bug",
                title="x",
                path=Path(d),
            )
            self.assertTrue(target.exists())


class DescriptionOptionalFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = FORM_JS.read_text(encoding="utf-8")

    def test_textarea_has_no_required_attribute(self) -> None:
        m = re.search(r'<textarea\s+id="fb-desc"[^>]*>', self.src)
        self.assertIsNotNone(m, "couldn't find the description textarea")
        self.assertNotIn("required", m.group(0),
                         "description textarea must not carry `required`")

    def test_submit_guard_does_not_block_on_empty_description_alone(self) -> None:
        # The old guard was `!title || !description`. Whichever symmetric
        # form lands, neither field on its own should block submit.
        self.assertNotIn("!title || !description", self.src,
                         "submit-guard still requires description even when title is filled")


if __name__ == "__main__":
    unittest.main()
