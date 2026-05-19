"""Unit tests for the email_files ledger + on-startup reindex.

Covers the refactor that treats .eml files in processed/ + failed/ as the
source of truth and the SQLite DB as a derived view. On every GUI start we
walk the archive, hash each file, and re-ingest anything whose sha256 or
parser_version doesn't match the recorded value — that way transient parse
failures get retried automatically and you never lose data because a
shipping email rotted in failed/ during an OpenAI outage.

Run: .venv/bin/python -m unittest tests.test_email_files_reindex -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailroom import db, inbox, parser


def _eml_text(
    subject: str = "Test order",
    sender: str = "vendor@example.com",
    body: str = "Order #ABC-123. Thank you for your purchase.",
) -> bytes:
    return (
        f"From: {sender}\r\n"
        f"To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 18 May 2026 12:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def _parsed_ok(
    *, order_number: str = "ABC-123", vendor: str = "TestVendor",
    tracking_number: str | None = None,
) -> parser.ParsedEmail:
    return parser.ParsedEmail(
        kind="order_confirmation",
        vendor=vendor,
        order_number=order_number,
        po_number=None,
        item_description="widget",
        ordered_date=None,
        ordered_date_confidence=0.0,
        promised_ship_date=None,
        promised_delivery_date=None,
        lead_time_days=None,
        tracking_number=tracking_number,
        carrier=None,
        tracking_url=None,
        status_signal="confirmed",
        confidence=0.9,
        notes=None,
    )


class IsolatedMailroom(unittest.TestCase):
    """Set up an isolated MAILROOM_INBOX + MAILROOM_DB for each test.

    Tests in this file MUST NOT touch the user's real ~/Mailroom — both the
    reindex and inbox helpers read inbox.inbox_dir() (env-overridable) and
    db.default_db_path() (env-overridable). We always override both, otherwise
    a stray run hits OpenAI on real .emls.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self._old_inbox = os.environ.get(inbox.INBOX_ROOT_ENV)
        self._old_db = os.environ.get("MAILROOM_DB")
        os.environ[inbox.INBOX_ROOT_ENV] = str(self.root)
        self._db_file = self.root / "test.sqlite"
        os.environ["MAILROOM_DB"] = str(self._db_file)
        db.init_schema()
        # MAILROOM_QUIET silences notify.send() — the reindex tests exercise
        # _ingest_one, which fires notify on status transitions if not gated.
        self._old_quiet = os.environ.get("MAILROOM_QUIET")
        os.environ["MAILROOM_QUIET"] = "1"

    def tearDown(self) -> None:
        if self._old_inbox is None:
            os.environ.pop(inbox.INBOX_ROOT_ENV, None)
        else:
            os.environ[inbox.INBOX_ROOT_ENV] = self._old_inbox
        if self._old_db is None:
            os.environ.pop("MAILROOM_DB", None)
        else:
            os.environ["MAILROOM_DB"] = self._old_db
        if self._old_quiet is None:
            os.environ.pop("MAILROOM_QUIET", None)
        else:
            os.environ["MAILROOM_QUIET"] = self._old_quiet
        self._tmpdir.cleanup()


class EmailFilesSchemaTests(IsolatedMailroom):
    """The email_files table must exist after init_schema() and be idempotent
    so a second call doesn't blow up — old installs will hit this code path
    once on first launch after upgrade."""

    def test_email_files_table_created(self):
        with db.connect() as conn:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(email_files)"
            ).fetchall()}
        self.assertEqual(
            cols,
            {"filename", "sha256", "parser_version", "parsed_at", "row_id", "error"},
        )

    def test_init_schema_is_idempotent(self):
        # A re-call should be a no-op, not a duplicate-table error.
        db.init_schema()
        db.init_schema()

    def test_upsert_and_get_roundtrip(self):
        db.upsert_email_file(
            filename="foo.eml",
            sha256="deadbeef",
            parser_version="v1+abc",
            row_id=None,
            error=None,
        )
        got = db.get_email_file("foo.eml")
        self.assertIsNotNone(got)
        self.assertEqual(got["sha256"], "deadbeef")
        self.assertEqual(got["parser_version"], "v1+abc")
        self.assertIsNone(got["row_id"])
        self.assertIsNone(got["error"])

        # Overwrite on conflict. row_id has to reference a real packages row
        # because the FK is enforced.
        pkg_id = db.add_package(description="x")
        db.upsert_email_file(
            filename="foo.eml",
            sha256="cafef00d",
            parser_version="v1+abc",
            row_id=pkg_id,
            error=None,
        )
        got = db.get_email_file("foo.eml")
        self.assertEqual(got["sha256"], "cafef00d")
        self.assertEqual(got["row_id"], pkg_id)


class ParserVersionTests(unittest.TestCase):
    """effective_parser_version() must be stable across calls and must change
    when PARSER_VERSION is bumped — those properties are the contract the
    reindex relies on to decide what's stale."""

    def test_stable_within_process(self):
        v1 = parser.effective_parser_version()
        v2 = parser.effective_parser_version()
        self.assertEqual(v1, v2)

    def test_changes_when_parser_version_bumps(self):
        v_before = parser.effective_parser_version()
        with mock.patch.object(parser, "PARSER_VERSION", "v999"):
            v_after = parser.effective_parser_version()
        self.assertNotEqual(v_before, v_after)
        self.assertTrue(v_after.startswith("v999+"))

    def test_format_is_version_plus_hash(self):
        v = parser.effective_parser_version()
        # "v<digits>+<10 hex chars>"
        self.assertIn("+", v)
        prefix, hashpart = v.rsplit("+", 1)
        self.assertEqual(prefix, parser.PARSER_VERSION)
        self.assertEqual(len(hashpart), 10)
        int(hashpart, 16)  # raises if not hex


class ReindexStartupTests(IsolatedMailroom):
    """End-to-end reindex behavior with the LLM mocked.

    These tests drive the same code path the FastAPI lifespan hook calls on
    every startup. The LLM is mocked everywhere — a real OpenAI call from a
    test would burn credits and make CI flaky.
    """

    def _drop_in_processed(self, name: str, body: bytes | None = None) -> Path:
        """Write a .eml into the processed/ dir without going through the
        ingest pipeline (so it has no email_files row yet — simulating an
        upgrade from a pre-ledger install)."""
        dirs = inbox._ensure_dirs()
        path = dirs["processed"] / name
        path.write_bytes(body or _eml_text())
        return path

    def _drop_in_failed(self, name: str, body: bytes | None = None) -> Path:
        dirs = inbox._ensure_dirs()
        path = dirs["failed"] / name
        path.write_bytes(body or _eml_text())
        return path

    def test_reindex_creates_row_for_unknown_eml(self):
        """A .eml in processed/ with no ledger entry must be re-ingested,
        land in processed/, and produce an email_files row pointing at the
        new packages row."""
        path = self._drop_in_processed("new.eml")
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            summary = inbox.reindex_all()
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["reparsed_success"], 1)
        self.assertTrue(path.exists(), "file should remain in processed/")

        entry = db.get_email_file("new.eml")
        self.assertIsNotNone(entry)
        self.assertIsNotNone(entry["row_id"])
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["parser_version"], parser.effective_parser_version())

        pkg = db.get_package(entry["row_id"])
        self.assertEqual(pkg["order_number"], "ABC-123")

    def test_reindex_skips_up_to_date_eml(self):
        """Same sha256 + same parser_version + row still present → skip.
        The LLM must NOT be called."""
        path = self._drop_in_processed("stable.eml")
        # Seed the ledger as if a previous run had parsed this file.
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            inbox.reindex_all()
        # Second pass: parse_email must not be called.
        with mock.patch.object(
            parser, "parse_email",
            side_effect=AssertionError("LLM should not be called on up-to-date eml"),
        ):
            summary = inbox.reindex_all()
        self.assertEqual(summary["skipped_current"], 1)
        self.assertEqual(summary["reparsed_success"], 0)
        self.assertTrue(path.exists())

    def test_reindex_reparses_when_parser_version_changes(self):
        """A bumped PARSER_VERSION must invalidate every cached entry."""
        self._drop_in_processed("stale.eml")
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            inbox.reindex_all()

        # Bump the version and confirm the LLM is invoked again.
        call_count = {"n": 0}

        def counting_parse(subject, sender, body):
            call_count["n"] += 1
            return _parsed_ok()

        with mock.patch.object(parser, "PARSER_VERSION", "v999"), \
             mock.patch.object(parser, "parse_email", side_effect=counting_parse):
            summary = inbox.reindex_all()

        self.assertEqual(call_count["n"], 1)
        self.assertEqual(summary["reparsed_success"], 1)

    def test_reindex_skips_received_rows(self):
        """The "received" terminal status is the user's sole manual input —
        never overwrite it from a re-parse, even when the ledger says the
        ingest is stale (different parser_version)."""
        self._drop_in_processed("frozen.eml")
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            inbox.reindex_all()

        entry = db.get_email_file("frozen.eml")
        self.assertIsNotNone(entry["row_id"])
        # User picked it up.
        db.update_package(row_id=entry["row_id"], status="received")

        # Even with a bumped version (which would normally force a re-ingest),
        # the row's received status must block the LLM call.
        with mock.patch.object(parser, "PARSER_VERSION", "v999"), \
             mock.patch.object(
                parser, "parse_email",
                side_effect=AssertionError("must not re-parse received rows"),
             ):
            summary = inbox.reindex_all()

        self.assertEqual(summary["skipped_received"], 1)
        self.assertEqual(summary["reparsed_success"], 0)
        # Status untouched.
        pkg = db.get_package(entry["row_id"])
        self.assertEqual(pkg["status"], "received")

    def test_reindex_failure_preserves_existing_row_id(self):
        """A re-ingest that crashes (transient OpenAI failure, etc.) must
        leave the existing packages row + email_files.row_id intact. The
        ledger gets an error message and the next launch tries again."""
        self._drop_in_processed("flaky.eml")
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            inbox.reindex_all()
        entry_before = db.get_email_file("flaky.eml")
        row_id = entry_before["row_id"]
        self.assertIsNotNone(row_id)

        # Force a stale parser_version so the scan tries to re-ingest, then
        # blow up inside parse_email.
        with mock.patch.object(parser, "PARSER_VERSION", "v999"), \
             mock.patch.object(
                parser, "parse_email",
                side_effect=RuntimeError("simulated OpenAI outage"),
             ):
            summary = inbox.reindex_all()

        self.assertEqual(summary["reparsed_failed"], 1)
        entry_after = db.get_email_file("flaky.eml")
        self.assertEqual(
            entry_after["row_id"], row_id,
            "failed re-ingest must not orphan the existing packages row",
        )
        self.assertIsNotNone(entry_after["error"])
        # Underlying row still there.
        self.assertIsNotNone(db.get_package(row_id))

    def test_reindex_handles_duplicate_filename_across_dirs(self):
        """The same filename in BOTH processed/ and failed/ is a bug somewhere
        upstream; we log a warning, prefer processed/, and keep going."""
        self._drop_in_processed("dup.eml")
        self._drop_in_failed("dup.eml")
        with mock.patch.object(parser, "parse_email", return_value=_parsed_ok()):
            summary = inbox.reindex_all()
        self.assertEqual(summary["duplicate_filename"], 1)
        # We process the processed/ copy, not both.
        self.assertEqual(summary["scanned"], 1)


if __name__ == "__main__":
    unittest.main()
