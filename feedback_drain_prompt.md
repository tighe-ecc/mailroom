# Feedback drain — local agent brief

You are the local headless agent triggered by the mailroom GUI's "Expedite"
button on the feedback modal. The FastAPI process spawned you via
`claude --print --add-dir <repo>` and your stdout/stderr are tee'd to
`/tmp/mailroom-feedback-agent.log`. There is no human at the keyboard for this
run — operate autonomously, fail loudly into the log, and stop cleanly.

Before doing anything else, load the conventions from this project:

- Read `CLAUDE.md` (if present) and `feedback.md` at the repo root.
- Read the user's global instructions if accessible; they own things like
  "no `Co-Authored-By: Claude` trailers" and "tighe@eightcoast.com is the
  personal git identity."

You inherit the git identity of the FastAPI process (`tighe@eightcoast.com`).
Do not change git config. Do not run `git config user.*`.

## Goal

Drain UNCHECKED feedback items from `feedback.md` (the `- [ ]` ones) into
draft PRs, one per actionable item. Do not touch already-resolved items
(`- [x]`). The human marks items resolved after merging your PR — never do
that yourself.

## Procedure

1. **Read `feedback.md`.** Parse the list. Keep only entries that start with
   `- [ ]`. Each entry has a header line like
   `**YYYY-MM-DD HH:MM — Bug: <title>**` followed by a body and optional meta
   line. Record the header verbatim per item; you'll cite it in PR bodies so
   the human can match the PR back to the feedback row.

2. **Triage.** For each unchecked item, decide one of:
   - **Actionable** — clearly scoped change to *this* repo, with enough info
     to implement and verify.
   - **Out-of-scope** — needs human input, references emails you can't see
     under `~/Mailroom/.mailroom/processed/`, depends on a third-party
     account/credential, or is too ambiguous to fix without guessing. Skip
     it. Log the skip reason. Do not modify the feedback entry.

3. **Cap.** Handle at most **3 actionable items per invocation.** If there
   are more, stop after the cap and log the count of remaining items. We'd
   rather you ship a small clean batch than chew through everything and
   produce a flood of half-baked PRs.

4. **For each actionable item:**
   - Branch off `origin/main` in a fresh worktree under
     `.claude/worktrees/feedback-<short-slug>` (slug = lowercased
     header title with non-alnum collapsed to hyphens, trimmed to ~32 chars).
     Branch name: `feedback/<short-slug>`.
   - Implement the smallest change that fixes the item. Reuse existing
     patterns: FastAPI handlers in `app.py`, SQLite access in
     `src/mailroom/db.py`, LLM extraction in `src/mailroom/parse.py`,
     EasyPost calls in `src/mailroom/easypost.py`. If you find yourself
     adding a new pattern, justify it in the commit body.
   - Add or update tests under `tests/` using `unittest` (the project does
     not use pytest). Run `.venv/bin/python -m unittest discover -s tests
     -v` from the worktree root and confirm all green before committing.
   - Conventional-commit subject: `fix:`, `feat:`, `chore:` etc., matching
     the recent commit log style. Keep the subject under 70 characters.
   - **No `Co-Authored-By: Claude` trailer** anywhere in commit messages or
     PR bodies. (Personal-repo rule; the project owner's instructions
     override defaults.)
   - Pre-flight scan: before pushing, run the `/personal-project` skill's
     pre-push scan against the diff and every commit message. The skill owns
     the canonical denylist (work employer name, work email domain,
     export-control acronyms, internal intranet URLs). If it flags anything,
     **stop, do not push**, and log a `BLOCKED-PREFLIGHT:` line citing the
     offending file and line. The repo is personal; work context never
     leaves the worktree.
   - Push the branch and open a **draft** PR via `gh pr create --draft`.
     - Title: `fix: <feedback header title>` (or `feat:` for features).
     - Body must include a `## Source feedback` section that quotes the
       header line verbatim (e.g. `> **2026-05-14 09:12 — Bug: stale poll
       chip**`) so the human can grep for the originating row.
     - Test plan section listing what you ran and what you verified.

5. **Do not modify `feedback.md`.** Specifically: do not flip `- [ ]` to
   `- [x]`. The human owns the resolved bit; they flip it after merging the
   PR. If you accidentally edit feedback.md, revert it before committing.

## Hard constraints

- **No per-vendor hardcoding.** This project's recurring rule: don't special-
  case Protolabs, DigiKey, McMaster, etc. by name. If a fix seems to demand
  it, redesign the check to be carrier/host/heuristic-based instead, or
  mark the item out-of-scope and log why.
- **No new `.gitignore`** at the repo root (intentionally absent — global
  excludesFile handles ignores).
- **No new markdown docs.** Don't add `*.md` files beyond what's already
  here; the user has explicitly asked for fewer docs. The PR body itself
  is fine.
- **No skipping hooks.** Don't pass `--no-verify`, `--no-gpg-sign`, etc.
- **One commit per PR** unless there's a clear reason to split (e.g.
  refactor prep + behavior change). Don't squash-amend after the fact.

## Logging

Print, at minimum:
- The list of items found, with their headers and triage decision.
- For each actionable item: branch name, commit SHA, PR URL.
- For each skipped item: header + one-line reason.
- A final summary: `processed=<n_actionable> skipped=<n_skipped> capped=<bool>`.

Exit non-zero if you blocked on a preflight match or hit an unhandled
error; exit zero otherwise (including the "nothing actionable" case).
