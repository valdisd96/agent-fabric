# agent-fabric

A long-running Python service plus two control surfaces (Telegram bot first, web dashboard second) that drives the same `plan-exec → test-writer → review-pr` Claude-Code pipeline across N project repos.

Skills are generic Jinja2 templates rendered per-project from each project's `.fabric/config.yaml`, then committed back into the project's `.claude/skills/` for native Claude Code to pick up. The fabric provides registration, rendering, drift detection, polling, single-flight dispatch, retries, cycle limits, quota tracking, and human-gate notifications.

The first project it manages is [`teach-me-eng-bot`](https://github.com/valdisd96/teach-me-eng-bot), whose three-stage pipeline (`workflow.md`) is the worked example the fabric is shaped around. See [`DESIGN.md`](./DESIGN.md) for full architecture, decisions, and roadmap.

## Status

Phase 0 (extract & generalize) — in progress. Phases tracked as GitHub issues.

## Install (dev)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI

| Command | Phase | Purpose |
|---|---|---|
| `fabric register <repo-path>` | 0A | Validate `<path>/.fabric/config.yaml` and add to `~/.fabric/projects.yaml`. |
| `fabric sync <project> [--check]` | 0B | Re-render skill templates into `<project>/.claude/skills/`. `--check` exits non-zero on drift. |
| `fabric tick` | 1 | One-shot poll-and-dispatch (debug). |
| `fabric dispatch <project> <issue> <stage>` | 1 | Force-dispatch a stage. |
| `fabric status` | 1 | Text dump of the current queue. |
| `fabric pause [--reason]` / `fabric resume` | 1 | Toggle the global pause flag. |
| `fabric logs <project> <issue> [--follow]` | 1 | Tail agent logs. |

Phase-1 commands are stubbed (exit code 2) until the scheduler/dispatcher land.

`$FABRIC_HOME` overrides the default registry directory (`~/.fabric`); the systemd unit on the Pi sets it to `/var/lib/fabric`.

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
