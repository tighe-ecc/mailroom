"""Pin the feedback modal contract: required fields get a visible asterisk.

A user-filed feedback item said it wasn't clear which fields are required
in the feedback modal, and asked for the standard asterisk indicator.
Every form input carrying the `required` attribute should have a sibling
`*` marker in its label.

Run: .venv/bin/python -m unittest tests.test_feedback_form_required_indicator -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORM_JS = ROOT / "static" / "feedback-button.js"


def _label_for(src: str, field_id: str) -> str:
    m = re.search(rf'<label\s+for="{re.escape(field_id)}"[^>]*>(.*?)</label>',
                  src, flags=re.S)
    if not m:
        raise AssertionError(f"label for {field_id!r} not found")
    return m.group(1)


def _required_field_ids(src: str) -> list[str]:
    ids: list[str] = []
    for tag in re.finditer(r'<(?:input|textarea|select)\b[^>]*>', src):
        chunk = tag.group(0)
        if " required" not in chunk:
            continue
        id_match = re.search(r'\bid="([^"]+)"', chunk)
        if id_match:
            ids.append(id_match.group(1))
    return ids


class RequiredIndicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = FORM_JS.read_text(encoding="utf-8")

    def test_every_required_field_label_has_an_asterisk(self) -> None:
        required_ids = _required_field_ids(self.src)
        self.assertTrue(
            required_ids,
            "expected at least one `required` field in the form template; "
            "if everything is now optional this test should be removed",
        )
        for fid in required_ids:
            label_html = _label_for(self.src, fid)
            self.assertIn(
                'class="required-indicator"',
                label_html,
                f"label for required field {fid!r} is missing the asterisk indicator",
            )
            self.assertIn(
                "*", label_html,
                f"label for required field {fid!r} should render a literal `*`",
            )

    def test_required_indicator_styled(self) -> None:
        self.assertIn(
            ".required-indicator",
            self.src,
            "missing CSS rule for `.required-indicator`",
        )

    def test_indicator_hidden_from_assistive_tech(self) -> None:
        for occurrence in re.finditer(
            r'<span class="required-indicator"[^>]*>', self.src
        ):
            self.assertIn(
                'aria-hidden="true"',
                occurrence.group(0),
                "the `*` is decorative; screen readers already get the cue "
                "from the input's `required` attribute, so the span must "
                "be aria-hidden to avoid being read aloud",
            )


if __name__ == "__main__":
    unittest.main()
