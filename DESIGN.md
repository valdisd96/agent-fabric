# Agent Fabric — Design Plan

A reusable multi-project agent fabric that subsumes the per-repo orchestrator described in [`teach-me-eng-bot/orchestrator-plan.md`](https://github.com/valdisd96/teach-me-eng-bot/blob/main/orchestrator-plan.md). The fabric is a long-running service plus two control surfaces (web dashboard + Telegram bot) that drive the same `plan-exec → test-writer → review-pr` pipeline across N project repos.

This document supersedes [`teach-me-eng-bot/orchestrator-plan.md`](https://github.com/valdisd96/teach-me-eng-bot/blob/main/orchestrator-plan.md) once Phase 1 ships. Decisions inherited from the orchestrator plan are marked **(inherited)** and only re-litigated if the new shape changes them.

> **Status:** Phase 0 not started. The repo and this design exist; the four phase-tracking issues are filed. Build order is TG-first per the [Phased roadmap](#phased-roadmap) — Phase 1 ships the Telegram bot before the web dashboard (Phase 2). This is a conscious deviation from the original draft of this doc, where Phase 1 was the dashboard.

## Table of contents

1. [Goals](#goals)
2. [Non-goals](#non-goals)
3. [Architecture](#architecture)
4. [Repo layout](#repo-layout)
5. [Per-project layout & config](#per-project-layout--config)
6. [Skill template model](#skill-template-model)
7. [Decisions](#decisions)
8. [Phased roadmap](#phased-roadmap)
9. [Migration from the in-repo orchestrator](#migration-from-the-in-repo-orchestrator)
10. [Risks & open questions](#risks--open-questions)

---

## Goals

The fabric must:

1. **Drive the pipeline across N projects** (target: 6–8 within 6 months) from one process, one config dir, one set of credentials.
2. **Surface the queue** so you can see at a glance — on either laptop or phone — what's running, what's waiting, what needs your input.
3. **Make human gates one-tap.** Approving a PR, answering a clarification, flipping a label, posting a comment — all from the dashboard or Telegram, without `gh` incantations.
4. **Reuse generic skills** across projects via templates, while letting any project override any skill locally.
5. **Stay lightweight.** Single-user. SQLite. Runs on a small Linux VPC VM (replaces an earlier Pi-based plan; see Decision 13). No Postgres, no message queue, no Kubernetes.
6. **Be open-source-able later.** Project-specific config is data, not code. Auth abstraction is clean enough to swap PAT for GitHub App.

## Non-goals (v1)

- Multi-tenancy / team auth.
- Per-stage parallelism within a project.
- Self-filing issues from the fabric itself (Phase 4 of the orchestrator plan).
- Replacing GitHub as the source of truth — issues, comments, PRs, labels stay canonical on GH; the fabric mirrors and acts on them.
- A mobile app — Telegram is the phone surface.

---

## Architecture

### Overview

```
                    ┌────────────────────────────────────────────────┐
                    │           AGENT FABRIC (Linux VPC VM)          │
                    │                                                │
   laptop ──HTTPS──▶│  ┌──────────────────────────────────────────┐ │
   (Phase 2)        │  │  FastAPI  ──  REST + WebSocket           │ │
                    │  │  HTMX dashboard pages                     │ │
                    │  └──────────────────────────────────────────┘ │
                    │                     │                          │
   phone ──Telegram─┼──▶ ┌──────────────┐ │                          │
                    │    │ Telegram bot │─┤                          │
                    │    └──────────────┘ │                          │
                    │                     ▼                          │
                    │  ┌──────────────────────────────────────────┐ │
                    │  │  Core service (asyncio)                   │ │
                    │  │   • Scheduler  (60s tick)                 │ │
                    │  │   • Dispatcher (subprocess: claude -p)    │ │
                    │  │   • GitHub client (gh CLI wrapper)        │ │
                    │  │   • SQLite state                          │ │
                    │  │   • Project registry                      │ │
                    │  │   • Skill renderer (Jinja2)               │ │
                    │  └──────────────────────────────────────────┘ │
                    │                     │                          │
                    └─────────────────────┼──────────────────────────┘
                                          │
                  ┌──────────────────┬────┴────┬──────────────────┐
                  ▼                  ▼         ▼                  ▼
          ┌─────────────┐    ┌─────────────┐  ...        ┌─────────────┐
          │  project 1  │    │  project 2  │             │  project N  │
          │  + .fabric/ │    │  + .fabric/ │             │  + .fabric/ │
          └─────────────┘    └─────────────┘             └─────────────┘
                  │                  │                          │
                  └──────────────────┴──────────────────────────┘
                                     │
                              ┌──────▼──────┐
                              │  GitHub API │
                              └─────────────┘
```

### Components

**Core service** — single process, asyncio event loop. Hosts everything:
- **Scheduler** — APScheduler-driven 60s tick. Iterates registered projects, queries GH for actionable issues, picks one per tick (cross-project priority).
- **Dispatcher** — spawns `claude -p --model claude-opus-4-7 ...` as `asyncio.create_subprocess_exec`. Streams stdout to log file + WebSocket subscribers. Single-flight via `asyncio.Semaphore(1)`.
- **GitHub client** — thin wrapper around `gh` CLI subprocess (already installed and authed; don't reinvent). Caches repo lookups.
- **State store** — SQLite at `~/.fabric/state.db`. Schema in [Decision 1](#decision-1--state-storage).
- **Project registry** — `~/.fabric/projects.yaml`. List of `{name, path, repo}` triples.
- **Skill renderer** — Jinja2 takes `<fabric>/skill_templates/<name>.md.j2` (overlaid by `<project>/.fabric/skills/<name>.md.j2` if present), renders with project config, writes to `<project>/.claude/skills/<name>.md`.

**Web dashboard** — HTMX + Tailwind, server-rendered from FastAPI. Pages:
- `/` — kanban board across all projects (columns = states, swim-lanes = projects)
- `/p/<project>` — project detail: queue, recent dispatches, agent logs
- `/p/<project>/i/<issue>` — issue detail: body + comments + plan-exec spec block + linked PR diff link, action panel (approve / request-changes / comment / change label / force-dispatch / block)
- `/live` — currently-dispatched agent's stdout, tailing
- `/settings` — pause/resume, project registry, polling interval, quota status

**Telegram bot** — push-notify + chat-control. Sends a message when an issue enters any human-gate state; inline buttons for the obvious next action. Reply-to-message becomes a GH comment. Slash commands: `/queue`, `/status`, `/pause`, `/resume`, `/projects`.

**CLI** — `fabric` command, mostly for setup and debug:
```
fabric register <repo-path>          add a project to the registry
fabric sync     <project> [--check]  re-render skills from templates
fabric tick                          one-shot poll-and-dispatch (debug)
fabric dispatch <project> <issue> <stage>   force-dispatch
fabric status                        text dump of current queue
fabric pause / fabric resume         flip the pause flag
fabric logs     <project> <issue>    tail agent logs
```

---

## Repo layout

The fabric lives in its own repo (working name: `agent-fabric`).

```
agent-fabric/
├── pyproject.toml
├── README.md
├── fabric/
│   ├── __init__.py
│   ├── cli.py
│   ├── server.py             # FastAPI app
│   ├── scheduler.py          # poll loop
│   ├── dispatcher.py         # claude -p subprocess
│   ├── github.py             # gh CLI wrapper
│   ├── state.py              # SQLite schema + DAO
│   ├── registry.py           # project registry
│   ├── render.py             # Jinja2 skill rendering
│   ├── telegram_bot.py
│   └── web/
│       ├── templates/        # Jinja2 HTMX templates
│       └── static/
├── skill_templates/
│   ├── plan-exec.md.j2
│   ├── test-writer.md.j2
│   ├── review-pr.md.j2
│   ├── clarify-issue.md.j2
│   ├── epic-decompose.md.j2
│   └── qualify-issue.md.j2
├── examples/
│   └── teach-me-eng-bot.config.yaml
├── scripts/
│   ├── install-systemd.sh
│   └── setup-labels.sh       # provisioned per-project, copied from here
└── tests/
```

---

## Per-project layout & config

Each managed project gets a `.fabric/` directory and (optionally) committed rendered skills.

```
my-project/
├── ...                              # existing project files
├── .fabric/
│   ├── config.yaml                  # required
│   └── skills/                      # optional per-project overrides
│       └── review-pr.md.j2          # overlays the fabric default for this skill
├── .claude/
│   └── skills/                      # rendered output (committed; see Decision 4)
│       ├── plan-exec.md
│       ├── test-writer.md
│       ├── review-pr.md             # rendered from the project's overlay
│       ├── clarify-issue.md
│       ├── qualify-issue.md
│       └── epic-decompose.md
```

### `.fabric/config.yaml` schema (sketch)

```yaml
project:
  name: teach-me-eng-bot
  repo: valdisd96/teach-me-eng-bot
  trusted_authors: [valdisd96]

build:
  setup_cmd: "source .venv/bin/activate"
  test_cmd: "python -m pytest -q"
  lint_cmd: null

modules:
  - path: bot.py
    role: "python-telegram-bot wiring + handlers + scheduler bootstrap"
  - path: vocab.py
    role: "vocab CRUD, mention scanning, FSRS rating, weighted-random select_word"
  - path: scheduler.py
    role: "push planning + APScheduler runner"
  # ...

safety:
  blocked_paths:
    - .github/workflows/**
    - teach-me-eng-bot.service
    - install-service.sh
  destructive_db_patterns:
    - "DROP COLUMN"
    - "DROP TABLE"
    - "ALTER COLUMN .* DROP"
  notes: |
    db.py migrations may add columns but never drop. FSRS columns
    (stability, difficulty, state, step, due, reps, lapses, last_review)
    are load-bearing and must not be touched.

labels:
  state_prefix: "state:"
  type_prefix: "type:"
  area_prefix: "area:"
  area_labels: [bot, vocab, scheduler, llm, translator, config, db]

pipeline:
  cycle_limit: 5
  retry_count: 3
  retry_backoff_seconds: [60, 300, 900]
  daily_dispatch_cap: 30        # quota guard

fabric_version: "0.3.0"          # for sync drift detection
```

Templates reference these via `{{ build.test_cmd }}`, `{% for m in modules %}{{ m.path }} — {{ m.role }}{% endfor %}`, etc.

---

## Skill template model

### Render flow

1. Fabric reads `<project>/.fabric/config.yaml`.
2. For each skill template:
   - If `<project>/.fabric/skills/<name>.md.j2` exists → use it.
   - Else → use `<fabric>/skill_templates/<name>.md.j2`.
3. Render with project config as Jinja2 context.
4. Write to `<project>/.claude/skills/<name>.md`.
5. The project commits these files. `fabric sync --check` warns if they're stale relative to the templates (drift detection).

### Why render-and-commit, not render-at-dispatch

- Claude Code expects skills at a known path (`.claude/skills/`) — no `--add-dir` magic needed.
- Committed skills are visible during code review, version-controlled, and diffable.
- The "double source of truth" (template + rendered) is mitigated by `fabric sync --check` in CI: warn if rendered output doesn't match the template's render.
- Treat rendered skills as generated artifacts (like `.d.ts` or protoc output) — humans don't edit them; they edit the template or the overlay.

### Override grain

Override at the skill level (whole file), not at the section level. A project that needs to deviate writes its own full template in `.fabric/skills/`. Section-level inheritance gets surprising fast and there's no demand for it yet.

### Drift detection in CI

A simple GitHub Action in each managed project: `fabric sync --check && git diff --exit-code .claude/skills/`. Fails the build if the committed skills don't match what the current fabric version would render. Forces an explicit `fabric sync` commit.

---

## Decisions

### Decision 1 — State storage

| | Approach | Notes |
|---|---|---|
| **A1** | SQLite (`~/.fabric/state.db`) **+ HTML comments mirrored to issues** | DB for fast cross-project queries (dashboard); comments for portability + survival of fabric reinstall. |
| A2 | SQLite only | Loses cycle counters etc. on host migration. |
| A3 | HTML comments only (current orchestrator plan) | Slow for dashboard rendering; N round-trips per page load. |

**Choice: A1.** SQLite is the working store; the cycle counter is *also* written to the issue HTML comment so a re-installed fabric can recover state from GH.

Schema sketch:

```sql
CREATE TABLE projects (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,         -- absolute path on host
  repo TEXT NOT NULL,         -- "owner/repo"
  fabric_version TEXT,
  registered_at TEXT NOT NULL
);

CREATE TABLE issues (
  project TEXT NOT NULL,
  number INTEGER NOT NULL,
  state_label TEXT,           -- snapshot from GH
  type_label TEXT,
  priority_label TEXT,
  area_label TEXT,
  title TEXT,
  url TEXT,
  cycle_count INTEGER DEFAULT 0,
  last_seen_at TEXT,
  PRIMARY KEY (project, number)
);

CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  issue INTEGER NOT NULL,
  stage TEXT NOT NULL,        -- plan-exec | test-writer | review-pr | epic-decompose | qualify-issue
  started_at TEXT NOT NULL,
  ended_at TEXT,
  exit_code INTEGER,
  log_path TEXT,
  triggered_by TEXT           -- "scheduler" | "manual:<surface>"
);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,         -- clarification | decompose-approval | blocked | dispatch-failed
  project TEXT NOT NULL,
  issue INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  delivered_to TEXT,          -- comma list of surfaces
  acknowledged_at TEXT
);

CREATE TABLE quota_log (
  project TEXT NOT NULL,
  day TEXT NOT NULL,          -- YYYY-MM-DD UTC
  dispatches INTEGER DEFAULT 0,
  PRIMARY KEY (project, day)
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
-- e.g. ('paused', '0'), ('paused_reason', '...')
```

### Decision 2 — Process model

| | Approach | Pros | Cons |
|---|---|---|---|
| B1 | bash + tmux (orchestrator plan) | Simple. | Hard to expose REST + WS. |
| **B2** | **Single asyncio Python process under systemd** | One process owns scheduler + API + WS + Telegram. Easy state sharing. | Restart blips kill in-flight dispatches. |
| B3 | Multi-process (scheduler + API + Telegram separate) | Crash isolation. | IPC/coordination complexity. SQLite locking gotchas. |

**Choice: B2.** With single-flight concurrency and short dispatch frequencies (~minutes per stage), restart blips are tolerable. SQLite is happy with one writer. The simplification of "one process, one log, one PID to manage" outweighs crash isolation for a single-user fabric.

Subprocess for the actual `claude -p` agent dispatch — that's expected to be long-running (minutes), so it has to be out-of-process. Streamed stdout goes to log file + WebSocket fanout.

### Decision 3 — Concurrency & queue model

| | Model | v1 |
|---|---|---|
| **C1** | **Global single-flight; per-project queues feed it** | ✓ |
| C2 | Per-project parallelism (1 stage per project, N projects in parallel) | Phase 3 |
| C3 | Per-stage parallelism (1 plan-exec, 1 test-writer, 1 review-pr concurrent) | Phase 3 |

**Choice: C1.** Single global semaphore, takes work from a cross-project priority queue (see Decision 4). Quota considerations alone (Pro/Max session limits) push toward serialization with 6–8 projects.

### Decision 4 — Cross-project selection

When multiple projects have actionable work this tick, who goes first?

| | Algorithm | Behaviour |
|---|---|---|
| D1 | Per-project round-robin within state priority | Fair; no project starves. |
| D2 | Pure cross-project priority (any project's `priority:high in-review` beats any project's `priority:medium`) | Urgent stuff jumps repos. |
| **D3** | **Hybrid: state priority first (in-review > rework > tests-pending > needs-planning > epic / unqualified), then `priority:*` across projects, then round-robin between projects at same level, then createdAt** | Drains in-flight; respects priority; no starvation. |

**Choice: D3.** Same spirit as the in-repo plan's E3, generalized across projects. Round-robin tiebreak ensures one chatty project can't monopolise the fabric.

### Decision 5 — API surface

REST + WebSocket from the same FastAPI app.

```
GET    /api/projects                 list registered projects
GET    /api/projects/{p}/issues      queue for a project (cached from last poll)
GET    /api/issues                   global queue
GET    /api/issues/{p}/{n}           issue detail (body, comments, latest plan, PR url)
POST   /api/issues/{p}/{n}/comment   {body} — posts as you, via gh
POST   /api/issues/{p}/{n}/label     {add: [...], remove: [...]}
POST   /api/issues/{p}/{n}/dispatch  {stage} — force-dispatch
POST   /api/prs/{p}/{n}/review       {action: approve|request-changes|comment, body}
POST   /api/prs/{p}/{n}/merge        squash + delete branch
GET    /api/dispatches?project=...   recent dispatch log
GET    /api/dispatches/{id}/log      tail logs (or WS)
POST   /api/pause                    {reason}
POST   /api/resume

WS     /ws/live                      stream of: dispatch_started, dispatch_stdout,
                                     dispatch_ended, queue_changed, notification_created
```

The dashboard and Telegram bot both consume this. Shared schemas (Pydantic models) live in `fabric/api_models.py`.

### Decision 6 — Web dashboard tech

| | Stack | Pros | Cons |
|---|---|---|---|
| E1 | SvelteKit / React SPA | Rich UX. | Build pipeline. Two languages. JWT plumbing. |
| **E2** | **HTMX + Tailwind + FastAPI Jinja2** | Server-rendered. No build step. Python-end-to-end. WebSocket integrates cleanly via `hx-ws`. | Less slick UX than SPA — fine for a single-user dev tool. |
| E3 | Pure HTML, full reload | Painful for live updates. | — |

**Choice: E2.** The dashboard is a control panel for one person; HTMX handles "kanban with live updates" cleanly without a build step. Switch to SvelteKit later if you ever build a public version.

### Decision 7 — Telegram bot scope

Push notifications for human-gate states + quick actions:

| Notification trigger | Buttons |
|---|---|
| Issue → `state:clarification-needed` | `Open in dashboard` `Mute issue` (reply to compose answer) |
| Issue → `state:awaiting-decompose-approval` | `Approve` `Reject` `Open` |
| Issue → `state:blocked` | `Open` `Mute` |
| PR opened by fabric | `Approve` `Request changes` `Open` |
| Dispatch failed (after retries) | `Retry` `Block` `Open` |
| Daily quota at 80% | `Pause project X` `Open settings` |
| Issue closed (PR merged or completed) | — (informational) |
| Any other observed `state:*` transition | — (informational, includes prev→new, cycle count, attribution) |

Slash commands:
- `/queue` — text-render of cross-project queue
- `/status` — what's running, last dispatch, paused?
- `/pause [reason]` / `/resume`
- `/projects` — list registered projects
- `/issue <project> <n>` — quick view

Inline reply: replying to a notification message that's tied to an issue posts the reply as a GH comment on that issue (and, for `clarification-needed`, flips the label to resume the pipeline — "/resume magic" promoted from the orchestrator plan's K3 to v1, since Telegram makes it natural).

### Decision 8 — Auth

| | Approach | v1 | v2 (open source) |
|---|---|---|---|
| F1 | One PAT with `repo` scope across all repos | ✓ | — |
| F2 | GitHub App, installed per-repo | — | ✓ |

**Choice: F1 for v1, F2 when open-sourcing.** PAT keeps setup zero-touch for a single user. GitHub App becomes worthwhile when (a) you want to share the fabric and not hand out PATs, (b) you want webhooks (Decision 9), or (c) you want fine-grained permissions per repo.

Dashboard auth (Phase 2 only — Phase 1 has no inbound HTTP): HTTP basic auth + private-network-only ingress. The VPC VM's internal network or a Tailscale exit-node both work; **never expose the dashboard to the public internet** — see Risks.

Telegram auth: bot only responds to a single hardcoded `chat_id` (yours). Anyone else gets ignored.

### Decision 9 — Trigger: polling vs webhooks

| | Approach | Phase |
|---|---|---|
| G1 | Polling, 60s | v1 (inherited) |
| G2 | GH webhooks → fabric `/webhook` endpoint | v3 |

**Choice: polling in v1.** Webhooks become attractive once the dashboard URL is stable on Tailscale and you've installed a GitHub App for auth — at that point, the marginal cost is one endpoint and a HMAC check, in exchange for sub-second latency on label flips. Defer until polling latency actually annoys you.

### Decision 10 — Quotas & throttling

With 6–8 projects, Claude Pro/Max session quotas become a real constraint. The fabric tracks per-project dispatch counts in `quota_log` and:

- **Per-project daily cap** — `pipeline.daily_dispatch_cap` in config. Default 30/day. Once hit, the project is skipped until UTC rollover; banner shown in dashboard; Telegram notification at 80%.
- **Global pause-on-quota-warning** — optional. If you've burned, say, 80% of weekly budget across all projects, fabric auto-pauses with a Telegram notice. (Manual estimate v1; no API to query Anthropic budget directly.)
- **Sonnet downgrade** — config flag per project: `pipeline.downgrade_low_priority: true` makes `priority:low` issues dispatch with `--model claude-sonnet-4-6`. Off by default.

### Decision 11 — Pause / resume

Three layers:

| Layer | Use case |
|---|---|
| **DB flag** (`settings.paused = '1'`) | Normal pause from dashboard / Telegram / CLI |
| **Per-project pause** (`projects.paused`) | Pause one project, leave others running |
| **Touch-file** (`~/.fabric/PAUSED`) | Hard escape hatch when the fabric itself is misbehaving and you can't reach the API |

Scheduler checks all three each tick. Pause is graceful — in-flight dispatch finishes; next tick is a no-op until resumed.

### Decision 12 — Failure handling

Inherited from orchestrator plan G2: 3 retries with backoff (60s, 5min, 15min), then park to `state:blocked` with a comment listing exit code + log path. Difference from the bash version: the comment is composed by the fabric (one place to update wording) and a Telegram notification fires at the park.

### Decision 13 — Deployment

Single systemd unit on a generic Linux VPC VM (Debian/Ubuntu). The earlier
Pi-on-Tailscale plan is dropped: the VM runs in a private VPC with outbound
internet for `gh` / `claude` / Telegram polling, and Phase 1 needs **no**
inbound HTTP — the Telegram bot is long-poll. Phase 2's dashboard adds
inbound, at which point a private ingress (VPC-internal LB or Tailscale
exit-node) takes over. The unit is provisioned by `scripts/install-systemd.sh`
(idempotent; creates the `fabric` system user, drops a 0600 `EnvironmentFile`,
enables but does not auto-start the service).

```ini
[Unit]
Description=Agent Fabric
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=fabric
WorkingDirectory=/srv/agent-fabric
EnvironmentFile=/etc/fabric/env
ExecStart=/srv/agent-fabric/.venv/bin/fabric server
Restart=always
RestartSec=5
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/var/lib/fabric

[Install]
WantedBy=multi-user.target
```

`/etc/fabric/env` carries `FABRIC_HOME`, `FABRIC_HOST`, `FABRIC_PORT`,
`FABRIC_TELEGRAM_TOKEN`, `FABRIC_TELEGRAM_CHAT_ID`. See SMOKE.md for the
full bring-up walkthrough on a fresh VM.

### Decision 14 — CLI modes

| Mode | Purpose |
|---|---|
| `fabric server` | Default — long-running service (what systemd runs) |
| `fabric tick` | One-shot tick, exits. Dry-run mode by default unless `--apply`. |
| `fabric register <path>` | Add project; validates `.fabric/config.yaml`; renders skills. |
| `fabric sync <project> [--check]` | Re-render skills; `--check` exits non-zero if drift. |
| `fabric dispatch <project> <issue> <stage>` | Force-dispatch, ignoring labels. |
| `fabric status` | Text dump (good for SSH-only sessions). |
| `fabric pause [reason]` / `fabric resume` | Toggle pause flag. |
| `fabric logs <project> <issue> [--follow]` | Tail agent logs. |

---

## Phased roadmap

**Phase 0 — extract & generalize (1–2 weeks)**
- New `agent-fabric` repo.
- Move agent scripts from `teach-me-eng-bot/scripts/` and convert to Python.
- Define `.fabric/config.yaml` schema.
- Build skill renderer + `fabric sync` + `fabric register`.
- Migrate teach-me-eng-bot to consume the fabric. Verify parity by running stages manually.
- Old `scripts/agent-*.sh` left in place as fallback during cutover.

**Phase 1 — service + Telegram bot (2 weeks)**
- FastAPI scaffold (no UI yet — the API surface from [Decision 5](#decision-5--api-surface) is built but only the Telegram bot and CLI consume it in this phase).
- SQLite schema, scheduler tick loop, single-flight dispatcher, retries, cycle counter, quota tracking.
- python-telegram-bot wiring, single-chat auth.
- Notifications for all human-gate states (clarification, decompose-approval, blocked, dispatch-failed, quota warning).
- Inline buttons → API calls.
- Reply-to-message → GH comment (+ label flip on `clarification-needed`).
- Slash commands (`/queue`, `/status`, `/pause`, `/resume`, `/projects`, `/issue`).
- systemd unit on Pi behind Tailscale. Cut over from the (unbuilt) in-repo bash daemon.

**Phase 2 — web dashboard (2 weeks)**
- HTMX + Tailwind pages: kanban, project, issue, live, settings.
- HTTP basic auth + Tailscale-only ingress (never public internet).
- WebSocket fanout for live dispatches (`/ws/live`).
- Action audit log on destructive endpoints.
- Surfaces every Telegram action plus richer cross-project views.

**Phase 3 — multi-project hardening (after a few weeks of running)**
- Add 2–3 more real projects. Iterate on config schema based on what doesn't fit.
- Cross-project quota guard.
- Webhook receiver (replace polling).
- GitHub App auth.
- `fabric sync --check` GH Action template.

**Phase 4 — open-source prep (when stable)**
- Docs, install script, example project config.
- Strip personal config (chat_id, repo names) from defaults.
- License (MIT or Apache-2.0).
- Public repo.

---

## Migration from the in-repo orchestrator

The current plan in `orchestrator-plan.md` was never fully built (only the agent scripts and skills exist). Migration is mostly "skip the bash daemon, build the Python service instead":

1. Phase 0 of the fabric replaces orchestrator-plan.md decisions wholesale.
2. The agent scripts (`scripts/agent-*.sh`) become Python functions in `fabric/dispatcher.py` calling `claude -p` directly. They stay on disk during transition as a manual fallback.
3. The skills under `.claude/skills/` are kept as-is in teach-me-eng-bot for one cycle, then converted to templates in the fabric repo and re-rendered into teach-me-eng-bot's `.claude/skills/` from there.
4. `scripts/setup-labels.sh` moves to the fabric and is invoked via `fabric register`.
5. `orchestrator-plan.md` is marked superseded once Phase 1 ships; deleted once Phase 2 ships.

---

## Risks & open questions

### Risks

1. **Dashboard-as-RCE.** The dashboard can dispatch agents, merge PRs, and edit labels across N repos. If exposed publicly or auth fails, it's effectively a remote-code-execution surface. Mitigations (Phase 2): private-VPC ingress only, basic auth, action audit log in DB, rate limit on destructive endpoints. (Phase 1 has no inbound HTTP at all — Telegram is long-poll outbound.)
2. **Single point of failure.** Fabric crashes → no project ships. Mitigations: systemd restart, SQLite state survives restart, health endpoint pinged by external uptime monitor.
3. **Skill-template drift across projects.** Updating a template ripples to all 6–8 projects. A bad template change ships bad PRs everywhere. Mitigations: `fabric sync --dry-run` shows diff per project; `fabric_version` in config pins compatibility; CI drift check fails the build on stale skills.
4. **Quota explosion.** 6–8 projects × pipeline activity can burn weekly Pro/Max budget faster than you notice. Mitigations: per-project daily cap, Telegram alert at 80% of cap, optional Sonnet downgrade for low-priority work.
5. **In-flight dispatch lost on restart.** Fabric restart kills the running `claude -p` subprocess. The issue stays at whatever label it was at; next tick may re-dispatch. Tolerable — agents are mostly idempotent (cycle counter prevents thrashing) but worth tracking.
6. **GH PAT scope.** A single PAT with `repo` across 6–8 repos is a juicy credential. v2 GitHub App reduces blast radius.

### Open questions

1. **Skill genericization depth.** How parameterized do templates need to be? Modules + safety paths + test command get you most of the way. Are there project-specific *judgments* (like "FSRS columns are sacred") that resist templating and just need a free-text `notes` field the skill prompt embeds verbatim?
2. **Should the fabric clone projects itself, or assume they're already cloned at a known path?** v1 plan: assume cloned; `fabric register <path>` just records the path. v2 could `git clone` from `repo` if `path` doesn't exist.
3. **Dashboard live diff view of agent stdout — useful or noise?** Tailing agent logs while a stage runs is satisfying but probably useless 95% of the time. Build it minimally; expand if you find yourself wanting more.
4. **One Telegram bot or one per project?** One bot, threaded by project (each notification message tags `[project-name]`). Multi-bot = multi-token = multi-account hassle.
5. **Cycle counter — do we still mirror to HTML comment or drop it once SQLite is the source of truth?** Mirror in v1 for portability (host migration without state-loss); reconsider when stable.
6. **Webhook receiver: where does it live?** Same FastAPI app as dashboard, on a `/webhook` route with HMAC verification. Tailscale Funnel for the public ingress (the only public surface), or skip and rely on polling — which is what v1 does.
7. **`fabric_version` semantics.** When the fabric bumps a template, does it auto-PR the re-render to each project? Or just warn? v1: warn via `fabric sync --check` in CI. Auto-PR is a Phase 3 thought.
8. ~~**Naming.**~~ **Resolved: `agent-fabric`** — repo at https://github.com/valdisd96/agent-fabric.
