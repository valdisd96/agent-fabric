# agent-fabric — design

A Telegram-controlled Python service that orchestrates autonomous Claude-Code agent pipelines across multiple GitHub repositories.

Each managed project keeps its own pipeline definition in-repo: a set of `scripts/agent-*.sh` entry points (one per pipeline stage) plus the matching `.claude/skills/`. The fabric is project-agnostic — it polls GitHub for `state:*` label transitions, picks the next eligible issue per project, shells out to the project's mapped script, manages retries and the cycle counter, and pushes the human-gate events (clarifications, decompose approvals, agent failures, cycle-cap hits, auto-merges) to a Telegram bot for one-tap action from your phone.

The worked example is [`teach-me-eng-bot`](https://github.com/valdisd96/teach-me-eng-bot) — see its `workflow.md` and `orchestrator-plan.md` for the upstream design that this fabric generalises.

## Locked decisions

| Axis | Choice | Notes |
|---|---|---|
| Stack | Python 3.11+, FastAPI, SQLite | Continuity with the bots that the fabric manages. |
| Control surface | Telegram bot only (v1) | HTMX-driven web dashboard deferred — FastAPI is already there if/when wanted. |
| Dispatcher | Fabric calls per-project `scripts/agent-*.sh <issue#>` | Each project owns its pipeline. Fabric stays project-agnostic. |
| State | GitHub = source-of-truth; SQLite = local cache + queue/dispatch/retry log | Cycle counter persists in `<!-- cycle:N -->` HTML comments on the issue (matches teach-me-eng-bot's plan). |
| Hosting | Same VPS as the bots being managed | One host to maintain auth + secrets. Per-project flock; cross-project parallelism. |
| Project registration | Central `projects.yaml` on the fabric host | Operator config, hot-reloaded. Project repos stay clean of fabric concerns. |
| Trigger | Polling, default 60s | Per-project, per-state. Webhooks deferred. |
| Concurrency | Per-project flock; cross-project parallel | One in-flight stage per project at a time. |
| Selection | E3: `state:in-review` first, then `priority:*`, then createdAt | Drains in-flight before starting new work. |
| Failure handling | G2: retry at 60s / 5m / 15m, then `state:blocked` with comment | Surfaces real bugs; tolerates flakes. |
| Cycle limit | 5 round-trips → `state:blocked` (counter in HTML comment) | Counts both Stage-2 bounces and Stage-3 rejections. |
| Trust gate | Issue author must be in the project's `trust_authors`; TG sender must be in `ALLOWED_TG_USER_IDS` | Two layers — issue gating + control-surface gating. |
| Model | All agents on `claude-opus-4-7` | Single model for v1; no mixing. |

## Architecture

```
                       ┌──────────────────────────────────────────┐
                       │          FastAPI app (uvicorn)            │
                       │                                            │
   GitHub  ◀── poll ───┤  ┌─────────────────────────────────────┐  │
   GitHub  ─── push ───┤  │ poller (asyncio task, 60s default)  │  │
   GitHub  ─── push ───┤  │ for each project, for each state:    │──┼─── shell ───▶  scripts/agent-*.sh
                       │  │   gh issue list → SQLite runs        │  │                in clone_path
                       │  │   pick winner per E3                  │  │
                       │  └──────────────┬──────────────────────┘  │
                       │                 │                           │
                       │  ┌──────────────▼──────────────────────┐  │
                       │  │ dispatcher                           │  │
                       │  │   per-project asyncio.Lock + flock   │  │
                       │  │   spawn claude -p via subprocess      │  │
                       │  │   stream stdout/stderr to logs/<p>/   │  │
                       │  │   on exit → refresh state from GH     │  │
                       │  └──────────────┬──────────────────────┘  │
                       │                 │                           │
                       │  ┌──────────────▼──────────────────────┐  │
                       │  │ state (SQLite, WAL)                  │  │
                       │  │   projects, runs, dispatches,         │  │
                       │  │   retries, notifications              │  │
                       │  └──────────────┬──────────────────────┘  │
                       │                 │                           │
                       │  ┌──────────────▼──────────────────────┐  │
   Telegram  ◀────────▶│  │ tg adapter (python-telegram-bot)     │  │
                       │  │   commands + inline-button alerts    │  │
                       │  └─────────────────────────────────────┘  │
                       └──────────────────────────────────────────┘
```

### Components

- **Poller** — async background task. Per project, per watched state: runs `gh issue list --label state:<X> --json number,labels,author,createdAt`. Filters by `trust_authors`. Writes/updates `runs` rows. Picks at most one winner per project per tick using selection rule **E3**.
- **Dispatcher** — drains the per-project work queue. Per-project `asyncio.Lock` plus an OS-level `flock` on `data/locks/<project>.lock` so that a fabric crash + restart can't double-dispatch. Spawns `claude -p --model claude-opus-4-7 …` via the project's mapped script for the winning issue's state. Streams logs to `logs/<project>/<issue>/<stage>-<ts>.log`. On script exit, refreshes the issue's state from GitHub and writes a `dispatches` row.
- **State store** — SQLite (WAL mode, `PRAGMA foreign_keys=ON`). The fabric's truth for *its own* operational state (queue, retries, notifications). GitHub remains the truth for issue body / labels / comments — the fabric mirrors what it needs to render queue views fast and to detect transitions.
- **Telegram adapter** — `python-telegram-bot`. Bot token + allowlist read from env. Commands listed below. Posts inline-button cards to subscribed `notify_telegram_chat_ids` for each project.
- **Config loader** — reads `projects.yaml` on startup; watches the file with `watchfiles` and hot-reloads on change (logs the diff; refuses reloads that would break the schema).

## Project config (`projects.yaml`)

Operator-owned, lives next to the fabric (not in any project repo).

```yaml
defaults:
  cycle_cap: 5
  poll_interval_seconds: 60
  retry_backoff_seconds: [60, 300, 900]
  selection: [in-review, tests-pending, needs-rework, needs-planning]

projects:
  - name: teach-me-eng-bot
    repo: valdisd96/teach-me-eng-bot
    clone_path: /srv/agent-fabric/projects/teach-me-eng-bot
    trust_authors: [valdisd96]
    notify_telegram_chat_ids: [12345678]
    pipeline:
      - state: needs-planning
        script: scripts/agent-plan-exec.sh
      - state: needs-rework
        script: scripts/agent-plan-exec.sh
      - state: tests-pending
        script: scripts/agent-test-write.sh
      - state: in-review
        script: scripts/agent-review.sh
    # Optional pre-stage routes by issue type.
    pre_stages:
      - type_label: type:epic
        script: scripts/agent-epic-decompose.sh
        states: [needs-planning]
```

Adding a new project: `git clone` into `clone_path`, append a block, save. The fabric hot-reloads; new project starts being polled at the next tick.

## Telegram interface

### Commands

| Command | Effect |
|---|---|
| `/queue [project]` | List in-flight + queued runs across all projects, or filtered to one. |
| `/show <issue-url\|#>` | Print state, last dispatch, retry count, cycle count, latest agent log tail. |
| `/approve <issue>` | (clarification or decompose-approval issues) Flip the label back to the next workflow state and post `/decompose-ok` if decompose. |
| `/reject <issue> <reason>` | Post `reason` as an issue comment and flip to `state:blocked`. |
| `/comment <issue> <text>` | Post `text` as an issue comment as the gh-authenticated user. |
| `/relabel <issue> <label>` | Set/replace the `state:*` label. |
| `/retry <issue>` | Force-dispatch the current state's script regardless of cycle counter. Logs the override. |
| `/pause [project\|all]` | Touch `data/pause/<project>` (or `data/pause/all`); poller skips paused entries. |
| `/resume [project\|all]` | Remove the touch-file. |
| `/projects` | List registered projects + their watched states + per-project pause status. |
| `/help` | Print the command list. |

### Inline-button cards

The fabric DMs you with an action card on these triggers (one DM each):

- **Clarification posted** by `clarify-issue` → buttons: `Open issue`, `Approve (re-run)`, `Reject`.
- **Decompose approval needed** (`state:awaiting-decompose-approval`) → buttons: `Open issue`, `/decompose-ok`, `Reject`.
- **Agent failed after retries** → buttons: `Open log`, `Retry`, `Block`.
- **Cycle cap hit** → buttons: `Open issue`, `Block`, `Override (+1 cycle)`.
- **PR auto-merged** → buttons: `Open PR`, `Open commit`. (Informational.)

## SQLite schema (sketch)

```sql
CREATE TABLE projects (
  name TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  clone_path TEXT NOT NULL,
  config_json TEXT NOT NULL,            -- last-known projects.yaml block
  paused INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
  issue_number INTEGER NOT NULL,
  state_label TEXT NOT NULL,            -- state:* without prefix
  priority TEXT,                        -- priority:* without prefix
  type_label TEXT,                      -- type:* without prefix
  author TEXT NOT NULL,
  cycle_count INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL,
  UNIQUE(project, issue_number)
);

CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  state_label TEXT NOT NULL,            -- state when dispatched
  script TEXT NOT NULL,                 -- path that ran
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  log_path TEXT NOT NULL,
  outcome TEXT                          -- 'completed' | 'retry' | 'blocked'
);

CREATE TABLE retries (
  dispatch_id INTEGER PRIMARY KEY REFERENCES dispatches(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL,             -- 1, 2, 3
  scheduled_for TEXT NOT NULL
);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  issue_number INTEGER,
  kind TEXT NOT NULL,                   -- 'clarification' | 'decompose_approval' | 'agent_failed' | 'cycle_cap' | 'auto_merge'
  tg_chat_id INTEGER NOT NULL,
  tg_message_id INTEGER NOT NULL,
  sent_at TEXT NOT NULL
);
```

Forward-only column migrations applied at startup.

## Failure handling

- Script exits 0 → mark dispatch `completed`, refresh state from GitHub, idle until next tick.
- Script exits non-zero → schedule retry attempt 1 (60s), 2 (5m), 3 (15m). After attempt 3, post issue comment `agent-fabric: <stage> failed 3× — exit=<code>, log=<url>` and flip to `state:blocked`.
- Script timeout (default 30 min, per-stage overridable) → terminate the subprocess group, mark exit code as `-1` for retry purposes.
- Cycle counter at `state:needs-rework` reaches `cycle_cap` → flip to `state:blocked` with a comment listing prior PR URLs (read from issue timeline). No retry.
- Fabric crash mid-dispatch → on restart, dispatches without `finished_at` for >2× their stage's typical duration are marked `outcome='abandoned'`; the issue is re-eligible at next tick (state on GitHub is unchanged because the agent script is responsible for label transitions).

## Hosting / deployment

Target host: same VPS as `teach-me-eng-bot`.

Required:
- `git`, `gh` (with PAT, `repo` scope), `claude` CLI (Pro/Max OAuth, one-time browser login), `python3.11+`.
- Repo cloned to `/srv/agent-fabric`, `.venv` activated, requirements installed.
- `.env` populated:
  - `AGENT_FABRIC_TG_TOKEN` — bot token from BotFather (separate bot from the eng-bot)
  - `ALLOWED_TG_USER_IDS` — comma-separated TG user IDs allowed to issue commands
  - `GH_TOKEN` — same scope as `gh auth login` (or rely on `gh` CLI session)
- `projects.yaml` populated; one `git clone` per project at the listed `clone_path`.
- systemd unit installed:

```ini
[Unit]   Description=agent-fabric orchestrator
[Service] Type=simple
          ExecStart=/srv/agent-fabric/.venv/bin/uvicorn agent_fabric.main:app --host 127.0.0.1 --port 8090
          Restart=always
          User=agent-fabric
          WorkingDirectory=/srv/agent-fabric
          EnvironmentFile=/srv/agent-fabric/.env
[Install] WantedBy=multi-user.target
```

Logs:
- journald (`journalctl -u agent-fabric -f`)
- `logs/fabric.log` (rotated 5 MB × 5)
- `logs/<project>/<issue>/<stage>-<ts>.log` for each agent dispatch

## v1 scope (filed as GitHub issues)

1. **Skeleton** — FastAPI app, SQLite schema + migrations, `projects.yaml` loader with hot-reload, healthcheck, settings via env.
2. **Poller** — async loop, per-project per-state `gh issue list`, E3 selection, writes `runs`. Honours per-project pause touch-files.
3. **Dispatcher** — per-project async lock + flock, spawn `claude -p` via mapped script, log streaming, dispatch + retry rows.
4. **Cycle counter + retry policy** — `<!-- cycle:N -->` HTML comments, G2 retry backoff, `state:blocked` flip with comment on cap or final failure.
5. **Telegram adapter** — bot, allowlist, all commands and inline-button alert cards.
6. **Notifications** — wire the five trigger kinds to TG cards; ensure exactly-one delivery per event using the `notifications` table.
7. **systemd unit + log rotation** — service file, log handler config, install instructions in `README.md`.
8. **Smoke playbook** — register `teach-me-eng-bot`, file a tiny test issue (e.g. "add an empty test file"), watch one full cycle (plan-exec → test-writer → review-pr → merge) land via TG. Document gotchas.

Each ticket is sized to be one PR. Built in order — later tickets depend on earlier ones.

## Deferred to v2

- Per-issue worktrees (Archon-style) for parallelism *inside* a single project.
- Web dashboard (FastAPI + HTMX) for desktop browser control.
- `.fabric.yaml` self-service onboarding (project-declares-itself).
- Multi-user TG auth (per-project ACLs instead of global allowlist).
- Slack / Discord adapters.
- `--replay <project> <issue> <stage>` mode.
- Daily throttle / quota-aware pause.
- Self-filing agent issues (file new bug issues from observed failures).
- Webhook-driven trigger (only if 60s polling feels slow).

## References

- [`teach-me-eng-bot/workflow.md`](https://github.com/valdisd96/teach-me-eng-bot/blob/main/workflow.md) — the canonical example pipeline this fabric orchestrates.
- [`teach-me-eng-bot/orchestrator-plan.md`](https://github.com/valdisd96/teach-me-eng-bot/blob/main/orchestrator-plan.md) — single-project precursor to the design above.
- [Archon](https://github.com/coleam00/Archon) — TS/Bun workflow engine that informed the multi-platform-ingress and worktree-per-run ideas. The fabric deliberately skips Archon's YAML DAG engine; the pipeline is a fixed shape per project.
