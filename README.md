# agent-fabric

A long-running Python service plus two control surfaces (Telegram bot first, web dashboard second) that drives the same `plan-exec → test-writer → review-pr` Claude-Code pipeline across N project repos.

Skills are generic Jinja2 templates rendered per-project from each project's `.fabric/config.yaml`, then committed back into the project's `.claude/skills/` for native Claude Code to pick up. The fabric provides registration, rendering, drift detection, polling, single-flight dispatch, retries, cycle limits, quota tracking, and human-gate notifications.

The first project it manages is [`teach-me-eng-bot`](https://github.com/valdisd96/teach-me-eng-bot), whose three-stage pipeline (`workflow.md`) is the worked example the fabric is shaped around. See [`DESIGN.md`](./DESIGN.md) for full architecture, decisions, and roadmap.

## Status

Phases 0 + 1 shipped. Phase 2 (HTMX dashboard) is the next milestone. See
[`DESIGN.md`](./DESIGN.md) "Phased roadmap" and `CLAUDE.md` for the
running phase log.

For deploying onto a fresh VPS, follow the runbook in
[`.claude/skills/install/SKILL.md`](./.claude/skills/install/SKILL.md)
(or [`SMOKE.md`](./SMOKE.md) for the lighter conceptual pass). For
adding a brand-new managed project on top of an already-running fabric,
see [`.claude/skills/register-project/SKILL.md`](./.claude/skills/register-project/SKILL.md).

## Install (dev)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI

| Command | Purpose |
|---|---|
| `fabric register <repo-path>` | Validate `<path>/.fabric/config.yaml` and add to the project registry. |
| `fabric sync <project> [--check]` | Re-render skill templates into `<project>/.claude/skills/`. `--check` exits non-zero on drift. |
| `fabric setup-labels <project> [--check]` | Idempotently provision the canonical state/priority/type/area label set in the project's repo. |
| `fabric tick` | One-shot poll-and-dispatch (debug). |
| `fabric dispatch <project> <issue> <stage>` | Force-dispatch a pipeline stage on a specific issue. |
| `fabric diagnose <project> <deployment-id>` | Manually run `deploy-diagnose` against a failed deployment (re-runs after a `docs/deploy.md` edit, or when auto-dispatch missed). |
| `fabric status` | Text dump of the current queue + recent dispatches. |
| `fabric pause [--reason]` / `fabric resume` | Toggle the global pause flag. |
| `fabric logs <project> <issue> [--follow] [--pretty]` | Tail the latest dispatch log (raw JSONL or pretty transcript). |
| `fabric server` | Run the long-running service (REST + WS + scheduler tick). What systemd executes. |

`$FABRIC_HOME` overrides the default registry directory (`~/.fabric`);
the systemd unit (see `scripts/install-systemd.sh`) sets it to
`/var/lib/fabric`. Other env vars: `FABRIC_HOST`/`FABRIC_PORT` (REST
bind, defaults `127.0.0.1:7878`), `FABRIC_TELEGRAM_TOKEN` +
`FABRIC_TELEGRAM_CHAT_ID` (TG bot — both must be set or the bot is
disabled with a one-line warning), `FABRIC_LOG_LEVEL` (default `INFO`).

## Drift CI for managed projects

Each managed project should run a drift check on every PR to catch hand-edits to rendered skills.

```bash
mkdir -p .github/workflows
cp /path/to/agent-fabric/examples/github-actions/fabric-sync-check.yml \
   .github/workflows/fabric-sync-check.yml
# then edit the file and replace REPLACE_WITH_AGENT_FABRIC_COMMIT_SHA
# with the agent-fabric commit you want to pin to.
```

The workflow installs agent-fabric at the pinned SHA and runs `fabric sync . --check` against the project. A failure means someone edited a file under `.claude/skills/` directly instead of changing the template in agent-fabric — the fix is to change the template, run `fabric sync <project>` locally, and commit the regenerated files.

## Auto-deploy for managed projects

A second workflow ships an opt-in auto-deploy pipeline: every push to `main`
fetches the new commit on the host VM, refreshes the venv only if
`requirements.txt` changed, runs an optional `scripts/migrate.sh`, restarts
the systemd unit, smoke-checks, and writes `/var/lib/<project>/deploy.json`.

```bash
mkdir -p .github/workflows
cp /path/to/agent-fabric/examples/github-actions/deploy.yml \
   .github/workflows/deploy.yml
# then edit the five CONFIGURE: values at the top of the file.
```

One-time host setup — registering a repo-scoped self-hosted runner, picking
an install directory the runner can write to, and creating
`/var/lib/<project>/` — is documented in
[`examples/runbooks/deploy-setup.md`](examples/runbooks/deploy-setup.md).

Deploy failures intentionally do **not** auto-rollback. The broken version
stays running while the fabric's `deploy-diagnose` skill reads the failure
bundle, the project's `docs/deploy.md`, and `git log <last_good>..<failed>`,
then files a properly-labeled GH issue (`state:needs-planning`,
`priority:high`, `type:bug`, `area:deploy`) that the existing pipeline picks
up. The fix-PR's merge auto-deploys and supersedes the broken version.

Auto-dispatched on `POST /api/projects/<n>/deploy-failures`; runnable
manually via `fabric diagnose <project> <deployment-id>` (useful for
re-running after updating `docs/deploy.md`). See DESIGN.md
"Decision 15 — Deployment of managed projects" for the full shape and
rationale.

Bump the pinned SHA intentionally to adopt fabric updates — you'll see the resulting skill diff in the same PR.

## Roadmap

- **Phase 0** — extract & generalize. Skill templates, `fabric register`, `fabric sync`, per-project `.fabric/config.yaml`. Migrate teach-me-eng-bot to consume the fabric.
- **Phase 1** — service + Telegram bot. Scheduler, dispatcher, SQLite state, retries, cycle counter, TG notifications + slash commands. Cuts over from the (unbuilt) bash daemon.
- **Phase 2** — web dashboard. HTMX kanban + action panel, Tailscale-only HTTPS.
- **Phase 3+** — multi-project hardening, GH App auth, webhook receiver, OSS prep.

## License

MIT (placeholder — confirm before publishing).
