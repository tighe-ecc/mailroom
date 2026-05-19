"""Append feedback entries to a project-local feedback.md.

Library:
    from feedback import note
    note("search returns wrong results", type="bug",
         title="Search special chars")

CLI:
    python -m feedback "the description text" --type bug --title "Title"
    python feedback.py "..." --type feature

Resolves the target feedback.md as follows:
- explicit ``path=`` argument wins
- otherwise, when imported, walk up from the caller's __file__ to the nearest
  directory that contains a feedback.md, or fall back to the caller's directory
- otherwise (CLI / __main__), use Path.cwd()

stdlib only.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from datetime import datetime
from pathlib import Path

VALID_TYPES = ("bug", "feature")


def _caller_dir() -> Path:
    """Best-effort directory of the importer (skips this module's frames)."""
    here = Path(__file__).resolve()
    for frame in inspect.stack()[1:]:
        f = frame.filename
        if not f or f == "<stdin>":
            continue
        try:
            p = Path(f).resolve()
        except OSError:
            continue
        if p == here:
            continue
        return p.parent
    return Path.cwd()


def _resolve_target(path: Path | str | None) -> Path:
    if path is not None:
        p = Path(path)
        return p if p.name == "feedback.md" else p / "feedback.md"

    start = _caller_dir()
    # Prefer an existing feedback.md in start or any parent (stop at the
    # filesystem root or once we leave the user's home, whichever first).
    home = Path.home().resolve()
    cur = start.resolve()
    while True:
        candidate = cur / "feedback.md"
        if candidate.exists():
            return candidate
        if cur.parent == cur or not str(cur).startswith(str(home)):
            break
        cur = cur.parent
    return start / "feedback.md"


def _format_entry(
    *,
    description: str,
    type: str,
    title: str | None,
    tool: str | None,
    url: str | None,
    expedited: bool = False,
    timestamp: datetime | None = None,
) -> str:
    ts = (timestamp or datetime.now()).strftime("%Y-%m-%d %H:%M")
    label = "Bug" if type == "bug" else "Feature"
    headline = f"{ts} — {label}"
    if title:
        headline += f": {title}"

    body_lines = [f"- [ ] **{headline}**"]
    desc = (description or "").strip()
    if desc:
        for line in desc.splitlines():
            body_lines.append(f"  {line}".rstrip())
    meta_bits = []
    if tool:
        meta_bits.append(f"tool: {tool}")
    if url:
        meta_bits.append(f"source: {url}")
    if expedited:
        meta_bits.append("expedited")
    if meta_bits:
        body_lines.append(f"  _{' · '.join(meta_bits)}_")
    return "\n".join(body_lines) + "\n"


def _ensure_file(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("# Feedback\n\n", encoding="utf-8")


def note(
    description: str,
    *,
    type: str = "bug",
    title: str | None = None,
    tool: str | None = None,
    url: str | None = None,
    expedited: bool = False,
    path: Path | str | None = None,
) -> Path:
    """Append a feedback entry. Returns the path written to."""
    if type not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}, got {type!r}")
    if not (description or "").strip():
        raise ValueError("description is required")
    target = _resolve_target(path)
    _ensure_file(target)
    entry = _format_entry(
        description=description,
        type=type,
        title=title,
        tool=tool,
        url=url,
        expedited=expedited,
    )
    with target.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    return target


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="feedback",
        description="Append a feedback entry to feedback.md in the current dir.",
    )
    parser.add_argument("description", help="Description text. Use - to read stdin.")
    parser.add_argument("--type", choices=VALID_TYPES, default="bug")
    parser.add_argument("--title", default=None)
    parser.add_argument("--tool", default=None)
    parser.add_argument(
        "--path",
        default=None,
        help="Target feedback.md path or directory (default: cwd).",
    )
    args = parser.parse_args(argv)

    description = args.description
    if description == "-":
        description = sys.stdin.read()

    # CLI: cwd wins over import-time caller resolution.
    target = Path(args.path) if args.path else Path.cwd()
    written = note(
        description,
        type=args.type,
        title=args.title,
        tool=args.tool,
        path=target,
    )
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
