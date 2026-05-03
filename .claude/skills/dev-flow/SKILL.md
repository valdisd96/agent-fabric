---
name: dev-flow
description: This skill should be used when starting any code change in this repo — enforces the branch → change → commit → PR → wait process. Invoke when the user asks to "fix", "add", "implement", "refactor", or make any modification under fabric/, skill_templates/, deploy/, examples/, tests/, or to top-level project files (pyproject.toml, README.md, DESIGN.md, .env.example).
version: 1.0.0
---

# dev-flow

The process for every code change in this repo. No exceptions unless the user explicitly says so.

## The flow

1. **Start from fresh main**

   ```bash
   git checkout main && git pull --ff-only
   ```

2. **Cut a branch** with a type prefix and a short kebab-case topic:

   - `feat/<topic>` — new behavior (new CLI subcommand, new TG command, new pipeline stage, etc.)
   - `fix/<topic>` — bug fix
   - `chore/<topic>` — deps, docs, deploy artifacts, non-code changes
   - `refactor/<topic>` — no behavior change

3. **Make the change.** Keep the diff focused on one concern — don't bundle unrelated edits.

4. **Smoke-check before committing.** Run whatever applies given what already exists in the repo:

   ```bash
   python -m py_compile $(git ls-files 'fabric/**/*.py' | tr '\n' ' ')   # syntax check on every Python file in fabric/
   ```

   If a venv + tests exist:
   ```bash
   source .venv/bin/activate && python -m pytest -q
   ```

   If `fabric/cli.py` is wired:
   ```bash
   fabric --help                  # CLI surface still imports cleanly
   ```

   Fix root causes — never skip.

5. **Commit.** Subject line ≤ 70 chars, imperative mood, body explains *why* not *what*.

6. **Push and open a PR against `main`:**

   ```bash
   git push -u origin HEAD
   gh pr create --base main --title "<title>" --body "<summary + test plan>"
   ```

7. **Wait.** The user merges manually — that is the approval gate. Never call `gh pr merge`.

8. **After merge**, clean up:

   ```bash
   git checkout main && git pull --ff-only && git branch -d <branch>
   ```

## Rules

- **Main is protected.** Never push directly to main. Never force-push to main.
- **`DESIGN.md` is the canonical contract.** If a code change diverges from DESIGN.md (architecture, decisions, schema, API surface), update DESIGN.md in the same PR. Don't let the doc drift behind the code — projects that depend on the fabric read DESIGN.md as the authoritative shape.
- **`.env` is never committed.** Secrets live only in `.env`. If adding a new env var, add it to `.env.example` and document it in `README.md`'s configuration table (or `CLAUDE.md` once it exists).
- **`.fabric/config.yaml` schema is a public contract.** Managed projects pin a `fabric_version` and depend on the schema's stability. Backwards-incompatible schema changes require: a migration note in the PR body, a version bump (semver minor for additive, major for breaking), and an `examples/teach-me-eng-bot.config.yaml` update.
- **Skill templates are load-bearing.** A change to `skill_templates/<skill>.md.j2` ripples to every managed project on their next `fabric sync`. PR body must list which projects render differently and why. Run `fabric sync --check` against `examples/teach-me-eng-bot/` to surface the diff before merging.
- **New CLI subcommands need three surfaces updated.** When adding (or renaming/removing) a `fabric` subcommand, update `fabric/cli.py`, `README.md`'s command table, and `DESIGN.md`'s "CLI modes" section. Skipping any leaves the command undiscoverable.
- **New Telegram commands need three surfaces updated.** When adding (or renaming/removing) a TG slash command (Phase 1+), update the bot's command registration in `fabric/telegram_bot.py`, the bot's `set_my_commands` autocomplete list, and `DESIGN.md`'s "Decision 7" command table.
- **Deploy artifacts need a note.** If touching `deploy/agent-fabric.service`, `deploy/install.sh`, or `deploy/upgrade.sh`, call it out in the PR — these affect the systemd-managed deployment on the Pi. A botched service file can keep the fabric down until you SSH in.
- **Cover all functionality with tests.** Every new code path, new function, or bug fix needs a test that would fail without your change. If the new logic sits in a hard-to-test layer (e.g. a TG handler in `fabric/telegram_bot.py` or a FastAPI route), refactor the testable piece out into a pure helper module (`fabric/render.py`, `fabric/state.py`, `fabric/scheduler.py`, etc.) and test it there — don't leave behavior uncovered just because the entrypoint is awkward to mock. The only acceptable gap is genuinely glue code whose only job is to wire tested helpers to an external framework.
- **Code style — structured, not clever.** Match the module layout planned in `DESIGN.md` ("Repo layout" section): `fabric/cli.py`, `fabric/server.py`, `fabric/scheduler.py`, `fabric/dispatcher.py`, `fabric/github.py`, `fabric/state.py`, `fabric/registry.py`, `fabric/render.py`, `fabric/telegram_bot.py`, `fabric/web/`. Keep pure helpers separate from I/O: logic that can be expressed as a function of its arguments should not open HTTP clients, hit SQLite, run subprocesses, or call the GitHub API — pass what it needs in, return what it computes out. Inject collaborators (`gh_client`, `now`, `subprocess_runner`, `sqlite_conn`) as keyword args with sensible defaults so tests can substitute fakes without monkeypatching. Avoid deep nesting — flatten with early returns / guard clauses. Prefer `dataclass` / `pydantic` models over bags of positional args. Type-annotate everything public, use `from __future__ import annotations`, and keep docstrings to one line summarizing *why*, not mechanical restatements of the signature.
- **Single-flight invariant.** The dispatcher uses an `asyncio.Semaphore(1)` for a reason — exactly one `claude -p` subprocess at a time across all projects. Anything that touches the dispatch path must preserve this invariant; loosening it is a Phase 3 design conversation, not a one-off PR.
- **The user decides when to merge.** Never auto-merge.
- **Don't squash or rebase published commits** without being asked.

## When to deviate

Only if the user explicitly says so — e.g. "just commit to main", "skip the PR". Note the deviation in the conversation.

## Notes for early Phase 0

This repo is at Phase 0 (extract & generalize) — most of the modules listed in the rules above don't exist yet. Until they do, the rules that mention them simply don't fire. New PRs in Phase 0 will mostly be additive: a new module landing, a CLI subcommand wiring, a skill template extracted from `teach-me-eng-bot`. Once the file exists, the rules around it kick in.
