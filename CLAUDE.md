# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

`agent-fabric` is a Python tool that drives the same `plan-exec → test-writer → review-pr` Claude Code pipeline across multiple project repos from one place. Today it ships the renderer and CLI; future phases add the long-running service + Telegram bot + web dashboard.

`DESIGN.md` is the canonical architectural contract. Decisions, schemas, phased roadmap, and risks all live there. If a code change diverges from it, update `DESIGN.md` in the same PR.

## Mental model — the renderer

Skills used to be hand-written Markdown files inside each project's `.claude/skills/`. Now they're **generated artifacts** built from Jinja2 templates plus a per-project YAML config:

```
fabric/skill_templates/<name>/SKILL.md.j2     ──┐
                                                ├──[fabric sync]──▶  <project>/.claude/skills/<name>/SKILL.md
<project>/.fabric/config.yaml                 ──┘                    (committed; what Claude Code reads)
```

Conceptually identical to SCSS → CSS or `protoc` output: humans edit the source, a build step produces the output, the output is committed so the consumer (Claude Code) finds it at a well-known path.

**Overlay mechanism.** A managed project may replace any single skill by writing its own `<project>/.fabric/skills/<name>/SKILL.md.j2`. That overlay beats the fabric default at the whole-skill level (no section-level inheritance — see DESIGN.md "Override grain"). teach-me-eng-bot has zero overlays today.

**The 5 fabric-managed skills:** `plan-exec`, `test-writer`, `review-pr`, `clarify-issue`, `epic-decompose`. The list lives at `fabric/render.py::SKILL_NAMES`. `dev-flow` is intentionally project-internal — `fabric sync` does not touch it.

## Repo layout

```
fabric/
├── cli.py           # typer app — register, sync (real); tick/dispatch/status/pause/resume/logs (Phase 1 stubs)
├── config.py        # pydantic v2 models for .fabric/config.yaml + load_config (strict, extra="forbid")
├── registry.py      # ~/.fabric/projects.yaml reader/writer + register(repo_path)
├── render.py        # Jinja2 env (StrictUndefined, keep_trailing_newline=True), overlay resolution, render_skill
├── state.py         # SQLite DAO at $FABRIC_HOME/state.db — schema_version + Decision-1 tables, forward-only migrations
├── sync.py          # iterates SKILL_NAMES, writes or --checks, SyncResult+SkillDrift with unified diffs
└── skill_templates/ # the 5 .j2 files — package data, ships in the wheel

examples/
├── teach-me-eng-bot.config.yaml         # the worked example; renders to teach-me-eng-bot's current skills byte-for-byte
└── github-actions/fabric-sync-check.yml # drift CI workflow projects copy into .github/workflows/

tests/
├── conftest.py        # shared fixtures (fabric_root, isolated_fabric_home, project, isolated_state_db)
├── fixtures/          # YAML fixtures for config/registry tests
├── test_config.py     # schema + load_config
├── test_registry.py   # ~/.fabric/projects.yaml CRUD
├── test_render.py     # overlay precedence, StrictUndefined, missing-template
├── test_sync.py       # write, idempotent, --check exit codes
├── test_cli_sync.py   # typer CliRunner end-to-end
├── test_state.py      # SQLite schema, migrations, DAO, FK cascade
└── test_parity.py     # byte-for-byte gate against teach-me-eng-bot (see below)
```

## Where things live

| Looking for… | Go to… |
|---|---|
| Architecture, decisions, phased roadmap | `DESIGN.md` |
| The development workflow rules | `.claude/skills/dev-flow/SKILL.md` |
| The 5 skill names the fabric ships | `fabric/render.py::SKILL_NAMES` |
| The config schema | `fabric/config.py` (pydantic v2 models) |
| Sample managed-project config | `examples/teach-me-eng-bot.config.yaml` |
| Drift CI workflow | `examples/github-actions/fabric-sync-check.yml` |

## Invariants

- **Schema is a public contract.** `.fabric/config.yaml`'s shape is depended on by every managed project (each pins a `fabric_version`). Backwards-incompatible changes require a version bump (semver minor for additive, major for breaking), a migration note in the PR body, and an `examples/teach-me-eng-bot.config.yaml` update.
- **Parity is byte-for-byte.** `tests/test_parity.py` renders the 5 templates against `examples/teach-me-eng-bot.config.yaml` and compares to `$TEACH_ME_ENG_BOT_PATH/.claude/skills/<name>/SKILL.md`. Any byte difference fails. Bumping a template's frontmatter `version:` is therefore a deliberate act gated by CI. Skips cleanly when `$TEACH_ME_ENG_BOT_PATH` is unset.
- **Templates ship inside the package.** `fabric/skill_templates/` (NOT repo root). `default_fabric_root()` returns the package dir so editable and wheel installs behave identically. Don't move them back out.
- **Skills are package data, not data files referenced by path.** Anything that needs to find them goes through `default_fabric_root()` or the `fabric_root` injection point — never `Path.cwd()` or repo-relative paths.

## Working in this repo

Use the `dev-flow` skill — branch → change → commit → PR → wait. The user merges manually.

```bash
# setup
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# tests
pytest -q                                                    # 71 tests, parity skipped
TEACH_ME_ENG_BOT_PATH=/path/to/teach-me-eng-bot pytest -q    # 76 tests, parity active

# CLI smoke
fabric --help

# end-to-end against the real sibling repo
export FABRIC_HOME=$(mktemp -d)
cp examples/teach-me-eng-bot.config.yaml /path/to/teach-me-eng-bot/.fabric/config.yaml
fabric register /path/to/teach-me-eng-bot
fabric sync teach-me-eng-bot                       # "already up to date"
fabric sync teach-me-eng-bot --check               # exit 0 / "is clean"
```

## Phase status

- **Phase 0 — done.** Issue #1 closed. Renderer, CLI (`register`, `sync`), 5 templates, parity gate, drift CI workflow, package data wiring all shipped.
- **Phase 1 — next.** Issue #2. Service + Telegram bot. Scheduler, dispatcher (subprocesses `claude -p`), SQLite state, retries, cycle counter, TG notifications + slash commands. The stubbed CLI commands (`tick`, `dispatch`, `status`, `pause`, `resume`, `logs`) become real here.
- **Phase 2 — web dashboard.** HTMX kanban + action panel.
- **Phase 3+** — multi-project hardening, GitHub App auth, webhook receiver.

Phase-1 CLI commands today are real-but-stubbed: they exit code 2 with "not implemented in phase 0". This is deliberate — the surface is discoverable and accidental invocations fail loudly.

## What's deliberately not parameterized yet

The schema validates many knobs (modules, blocked_paths, destructive_db_patterns, label prefixes, FSRS-style notes), but only `setup_cmd && test_cmd` is currently substituted into the templates (3 sites). The rest of the prose is verbatim from teach-me-eng-bot's skills. Deeper parameterization waits for a second project — that's where reality will tell us which prose actually needs to vary versus which is universal advice that just happens to use teach-me-eng-bot examples. For now, projects whose needs diverge would write a full skill overlay rather than the fabric inferring a generic template.

## What's intentionally out of scope for this repo

- The bash agent runners in `teach-me-eng-bot/scripts/agent-*.sh` keep working untouched until Phase 1 ports them to `fabric/dispatcher.py`.
- Committing `.fabric/config.yaml` + the drift workflow into teach-me-eng-bot itself is a teach-me-eng-bot-side change, not an agent-fabric change.
- Publishing to PyPI — until then, projects install via `pip install agent-fabric @ git+https://github.com/valdisd96/agent-fabric.git@<sha>`.
