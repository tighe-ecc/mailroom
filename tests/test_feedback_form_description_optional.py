"""Pin the feedback contract: description is optional.

The feedback library originally raised ``ValueError("description is
required")`` and the form's textarea carried the ``required`` attribute.
A user-filed feedback item asked for description to be optional so the
form stays as low-friction as possible.

This test guards three things:
  - ``feedback.note("")`` writes a row instead of raising
  - the form's textarea no longer has ``required``
  - the client-side guard does not block on empty description

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
                "   \n  ",
                type="bug",
                title="whitespace body",
                path=Path(d),
            )
            self.assertTrue(target.exists())
            self.assertIn("whitespace body", target.read_text(encoding="utf-8"))


class DescriptionOptionalFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = FORM_JS.read_text(encoding="utf-8")

    def test_description_textarea_has_no_required_attribute(self) -> None:
        m = re.search(r'<textarea\s+id="fb-desc"[^>]*>', self.src)
        self.assertIsNotNone(m, "couldn't find the description textarea")
        self.assertNotIn(" required", m.group(0),
                         "description textarea must not have `required`")

    def test_submit_guard_does_not_block_on_empty_description(self) -> None:
        self.assertNotIn(
            "Title and description are required.",
            self.src,
            "submit-guard message still implies description is required",
        )
        self.assertNotIn(
            "!title || !description",
            self.src,
            "submit-guard still rejects submissions when description is empty",
        )


if __name__ == "__main__":
    unittest.main()
