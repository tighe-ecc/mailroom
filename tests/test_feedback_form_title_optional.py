"""Pin the feedback modal contract: title is optional.

Background: the feedback form originally required both title and description.
User-filed feedback asked for title to be optional so the form stays as
lightweight as possible. This test guards against a regression that puts
``required`` back on the title input or re-adds title to the client-side
validation gate.

Run: .venv/bin/python -m unittest tests.test_feedback_form_title_optional -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "static" / "feedback-button.js"


class FeedbackFormTitleOptionalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = FORM_JS.read_text(encoding="utf-8")

    def test_title_input_has_no_required_attribute(self) -> None:
        m = re.search(r'<input\s+id="fb-title"[^>]*>', self.src)
        self.assertIsNotNone(m, "couldn't find the title input in feedback-button.js")
        self.assertNotIn(" required", m.group(0),
                         "title input must not have the `required` attribute")

    def test_submit_guard_does_not_block_on_empty_title_alone(self) -> None:
        # The old guard was `!title || !description` (block if either missing).
        # The bundled change makes both fields optional; the new guard is
        # `!title && !description` (block only if BOTH missing).
        self.assertNotIn("!title || !description", self.src,
                         "submit-guard still requires title even when description is filled")


if __name__ == "__main__":
    unittest.main()
