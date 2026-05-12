---
name: install
description: Step-by-step procedure for installing agent-fabric on a fresh Ubuntu 24.04 LTS VPS and registering the first managed project. Invoke when the user asks to "install", "deploy", "set up", "provision", or "bring up" agent-fabric on a server, VM, or VPS — or asks how to run it next to a managed project like teach-me-eng-bot. Project-internal; not rendered into managed projects by `fabric sync`.
version: 1.3.0
---

# install

Install `agent-fabric` on a fresh Ubuntu 24.04 LTS VPS, bring up the
systemd service, and register the first managed project. Companion to
`SMOKE.md` (the lightweight end-to-end smoke test) — this skill is the
*detailed* install runbook with the gotchas filled in.

If the user is on a different distro, stop and confirm — package names,
service paths, and the Python 3.12 default below assume Ubuntu 24.04 +
`apt` + `systemd`. Debian 12 ships Python 3.11 and won't satisfy the
`requires-python = ">=3.12"` pin without `pyenv` or building from source —
push back rather than improvising. If they're trying to run locally for
development, point them at `CLAUDE.md` ("Working in this repo") instead —
this skill is for the deployed service path.

## Deployment model

The service runs as **`root`** with `IS_SANDBOX=1`. This is intentional
and intended for single-tenant VMs or isolated one-time containers —
there is no separate `fabric` system user to switch to, no `sudo -u`
dance for auth, and Claude Code reads its credentials from `/root/.claude`.
If you need a hardened multi-tenant install with a dedicated service
user, that's outside the scope of this runbook.

## What you'll end up with

```
/srv/agent-fabric/          # checkout + .venv          (root:root)
/srv/projects/<name>/       # cloned managed repos      (root:root, 0750)
/var/lib/fabric/            # FABRIC_HOME (state.db, logs/)   (root:root, 0750)
/etc/fabric/env             # systemd EnvironmentFile   (root:root, 0600)
/etc/systemd/system/fabric.service
```

One systemd unit (`fabric.service`) serving REST on `127.0.0.1:7878` and
the Telegram bot inside the same process. Scheduler ticks every 60 s;
one `claude -p` subprocess at a time across all projects (single-flight
invariant — see DESIGN.md).

## Prerequisites — confirm before starting

Before running anything, confirm with the user that they have:

- A fresh Ubuntu 24.04 LTS VPS with root access (or sudo).
- A GitHub Personal Access Token with `repo` scope (and `workflow` if any
  managed project's `safety.blocked_paths` permits `.github/workflows/`
  edits — teach-me-eng-bot blocks them, so `repo` alone is enough there).
- A Telegram bot token from `@BotFather` and the user's numeric `chat_id`
  (point them at [@userinfobot](https://t.me/userinfobot) — usernames
  don't work, must be numeric). Optional — without these the service
  runs REST-only.
- The HTTPS clone URL of every project they want managed. Each project
  needs a `.fabric/config.yaml` checked in (or copied at register time).

If anything is missing, pause and ask — don't guess Telegram chat IDs or
fabricate PATs.

All commands below assume you're running as root (or prefixed with
`sudo` if you ssh in as a non-root user). For brevity, the snippets
elide `sudo`.

## Step 1 — system packages

```bash
apt-get update
apt-get install -y python3.12 python3.12-venv git curl jq ca-certificates
```

Ubuntu 24.04 ships Python 3.12 in the default repos, which matches the
`requires-python = ">=3.12"` pin in `pyproject.toml` and what CI tests
against — no PPA or deadsnakes needed.

Confirm the interpreter is on `$PATH` before continuing:

```bash
python3.12 --version       # e.g. Python 3.12.3
```

## Step 2 — install `gh` and `claude` CLIs

**`gh`** — recommended path is the apt repository (system-wide, auto-updates
via `apt upgrade`):

```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
chmod 0644 /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update && apt-get install -y gh
```

**`claude`** — recommended path is Anthropic's apt repository (binary
lands at a system path, no `$HOME` involvement):

```bash
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
  -o /etc/apt/keyrings/claude-code.asc
echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
  > /etc/apt/sources.list.d/claude-code.list
apt-get update && apt-get install -y claude-code
```

Verify the Anthropic key fingerprint before trusting:

```bash
gpg --show-keys /etc/apt/keyrings/claude-code.asc
# Must report: 31DD DE24 DDFA B679 F42D  7BD2 BAA9 29FF 1A7E CACE
```

The native curl|sh installer (`curl -fsSL https://claude.ai/install.sh
| bash`) also works, but it drops the binary under `/root/.local/share/claude/`.
That's fine for this root-mode install (the unit no longer enables
`ProtectHome=true`), but apt is cleaner.

Verify both:

```bash
gh --version
claude --version
```

## Step 3 — check out and install agent-fabric

```bash
install -d -m 0755 /srv/agent-fabric
git clone https://github.com/valdisd96/agent-fabric /srv/agent-fabric
cd /srv/agent-fabric
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
fabric --help            # sanity check — must list 11 subcommands
deactivate
```

If `pip install` errors with `Package requires a different Python:
3.x is not in '>=3.12'`, the venv was created with the wrong interpreter
— delete `.venv/` and re-run with `python3.12 -m venv` explicitly.

## Step 4 — install the systemd unit

```bash
bash /srv/agent-fabric/scripts/install-systemd.sh
```

The script is idempotent. It creates:

- `/var/lib/fabric` (mode 0750, owned by `root`) — `FABRIC_HOME`
- `/srv/projects` (mode 0750, owned by `root`) — managed-repo clones land here
- `/etc/fabric/env` with stub values plus `IS_SANDBOX=1`, mode 0600, owned by `root`
- `/etc/systemd/system/fabric.service` with `User=root`, plus
  `ProtectSystem=full`, `PrivateTmp=true`, `ReadWritePaths=$FABRIC_HOME`

It prints a warning if `claude` is not on `PATH`. The script does **not**
auto-start the service — auth comes first.

## Step 5 — authenticate `gh` and `claude`

The service runs as root, so credentials live in `/root/.config/gh/` and
`/root/.claude/`. Just run the login commands directly:

```bash
# gh — paste the PAT when prompted; answer "Yes" to "Authenticate Git"
gh auth login --hostname github.com

# claude — interactive OAuth; see "Headless claude login" below if no browser
claude login
```

Verify:

```bash
gh auth status
claude --version && ls -la /root/.claude
```

### Headless `claude login` (no browser on the VPS)

`claude login` prints an OAuth URL and waits on a local port (e.g.
`localhost:8000`). To complete the flow without a browser on the VPS:

1. From the user's laptop, open a forwarded SSH session in another
   terminal: `ssh -L 8000:localhost:8000 vps-host` (substitute the port
   `claude` printed).
2. Open the OAuth URL on the laptop. The callback hits
   `localhost:8000` on the laptop → the SSH tunnel forwards it to the
   VPS's listener → `claude` records the token.
3. `claude` prints "logged in"; close the tunnel.

If `claude login` is impossible (e.g. no laptop browser available),
stop and ask the user before continuing — the service will start but
every dispatch will fail.

## Step 6 — fill `/etc/fabric/env`

```bash
EDITOR=vi visudo -f /etc/fabric/env   # or just: nano /etc/fabric/env
```

Set the Telegram pair (omit both for REST-only mode):

```bash
FABRIC_HOME=/var/lib/fabric
FABRIC_HOST=127.0.0.1
FABRIC_PORT=7878
IS_SANDBOX=1                                          # baked in by installer
FABRIC_TELEGRAM_TOKEN=<from BotFather, looks like 1234567890:AA…>
FABRIC_TELEGRAM_CHAT_ID=<numeric, from @userinfobot>

# Optional:
# FABRIC_LOG_LEVEL=INFO              # DEBUG for ad-hoc troubleshooting
```

File must end up `root:root, 0600`. If the editor changed mode:

```bash
chown root:root /etc/fabric/env && chmod 0600 /etc/fabric/env
```

## Step 7 — start the service

```bash
systemctl start fabric
systemctl status fabric --no-pager     # expect: active (running)
journalctl -u fabric -f                # tick loop should log within 60s
```

If `status` shows `failed`, dump the last 50 lines and inspect:

```bash
journalctl -u fabric -n 50 --no-pager
```

Common first-boot failures:

| Log signature | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: fabric` | venv at wrong path | confirm `/srv/agent-fabric/.venv/bin/fabric` exists |
| `gh: command not found` (in dispatch logs) | `gh` not on root's `$PATH` | `ln -s "$(command -v gh)" /usr/local/bin/gh` |
| `Claude Code may not be run as root … set IS_SANDBOX=1` | env file missing `IS_SANDBOX=1` | add it, `systemctl restart fabric` |
| `OperationalError: unable to open database file` | `$FABRIC_HOME` missing/unwritable | `install -d -o root -g root -m 0750 /var/lib/fabric` |
| `telegram error: Unauthorized` | bad bot token | re-check `FABRIC_TELEGRAM_TOKEN`, restart |
| One-line warning about TG creds, then REST-only | both TG vars unset | expected; fill them and `systemctl restart fabric` |

## Step 8 — register the first managed project

`/srv/projects` was created by `install-systemd.sh` (Step 4). Clone,
configure, register, label, and sync — all as root.

The block below is shaped for **teach-me-eng-bot** specifically — it
copies the worked-example config from `examples/`. For any other project
you need an authored `.fabric/config.yaml` first; see the
`register-project` skill for the end-to-end "design a config from
scratch + register" walkthrough.

**Critical:** an interactive shell does *not* automatically source
`/etc/fabric/env`. Without `FABRIC_HOME`, the CLI writes the registry to
`~/.fabric/projects.yaml` — a path the systemd service does not read.
Export it explicitly. As of fabric 0.2.x the CLI emits a stderr warning
when this divergence is detected, so you'll notice quickly if you forget.

```bash
export FABRIC_HOME=/var/lib/fabric        # ← MUST match /etc/fabric/env
cd /srv/projects
git clone https://github.com/<owner>/<repo>
PROJECT=/srv/projects/<repo>

# Copy or write the .fabric/config.yaml
mkdir -p "$PROJECT/.fabric"
# For teach-me-eng-bot specifically:
cp /srv/agent-fabric/examples/teach-me-eng-bot.config.yaml \
   "$PROJECT/.fabric/config.yaml"

FABRIC=/srv/agent-fabric/.venv/bin/fabric
"$FABRIC" register "$PROJECT"                     # prints registry path
"$FABRIC" setup-labels <project-name> --check     # diff against repo
"$FABRIC" setup-labels <project-name>             # apply
"$FABRIC" sync <project-name>                     # renders 7 skill templates into .claude/skills/
```

Substitute `<owner>/<repo>` and `<project-name>` (the `project.name` from
`config.yaml`). After `sync`, the rendered skill files are *uncommitted*
in the project working tree — guide the user to commit them upstream:

```bash
cd /srv/projects/<repo> && git status
```

The user creates the commit + PR from their own clone, not the VPS — the
VPS clone is a working copy the dispatcher rebases against `origin/main`
on every cycle.

## Step 9 — smoke test (5 minutes, end-to-end)

Have the user open an issue in the managed repo with these labels:

- Title: `smoke: rename README heading` (or any trivial change)
- Body: a one-sentence diff description
- Labels: `state:needs-planning`, `priority:low`, `type:docs`,
  `area:<one of the project's area labels>`

Then watch progression in three windows:

```bash
journalctl -u fabric -f
/srv/agent-fabric/.venv/bin/fabric logs <project> <issue#> --follow
# Telegram: /queue and /status
```

Expected state transitions (D3 selection picks the issue within ~60 s):

```
state:needs-planning  →  state:in-progress     (plan-exec dispatches)
state:in-progress     →  state:tests-pending   (plan committed, branch pushed)
state:tests-pending   →  state:in-review       (test-writer runs, PR opens)
                      →  Telegram notification with Approve/Request changes
                      →  user taps Approve → squash merge, branch deleted
```

## Operations cheatsheet

```bash
# Service control
systemctl restart fabric
systemctl stop fabric
systemctl status fabric --no-pager

# Logs
journalctl -u fabric -f                            # service stdout/stderr
ls /var/lib/fabric/logs/                           # per-dispatch claude -p logs
/srv/agent-fabric/.venv/bin/fabric logs <project> <n> --follow

# REST surface (all behind /api/* except liveness; OpenAPI at /docs)
curl -sS http://127.0.0.1:7878/healthz             # {"ok": true}
curl -sS http://127.0.0.1:7878/api/status          # paused flag + counters
curl -sS http://127.0.0.1:7878/api/projects        # registered projects
curl -sS http://127.0.0.1:7878/api/issues          # all tracked issues
curl -sS http://127.0.0.1:7878/api/dispatches      # recent dispatches
curl -sS http://127.0.0.1:7878/docs                # interactive OpenAPI UI

# State (read-only is safe while running)
sqlite3 -readonly /var/lib/fabric/state.db '.tables'

# CLI escape hatches — always export FABRIC_HOME first
export FABRIC_HOME=/var/lib/fabric
F=/srv/agent-fabric/.venv/bin/fabric
"$F" pause --reason "demo"
"$F" resume
"$F" dispatch <project> <issue#> plan-exec
"$F" sync <project> --check         # drift check
```

## Upgrading

```bash
cd /srv/agent-fabric
git pull --ff-only
/srv/agent-fabric/.venv/bin/pip install -e .
systemctl restart fabric

# Re-render skills if templates moved (per managed project)
export FABRIC_HOME=/var/lib/fabric
/srv/agent-fabric/.venv/bin/fabric sync <project>
```

If a template's `fabric_version` bumped major and a managed project's
`.fabric/config.yaml` still pins the old major, `sync` errors with a
version mismatch — bump the pin in the project's YAML and re-sync.

## Hand-off checklist

Before declaring the install complete, verify all of:

- [ ] `systemctl is-active fabric` → `active`
- [ ] `journalctl -u fabric --since "1 minute ago"` shows scheduler tick
- [ ] `gh auth status` → logged in
- [ ] `claude --version` → no auth errors
- [ ] At least one project shows up in `fabric list`
- [ ] `setup-labels <project> --check` → "labels are clean"
- [ ] `sync <project> --check` → exit 0
- [ ] Telegram `/status` responds (if TG configured)
- [ ] Smoke issue cycled through all four states and merged

If any item fails, debug it before moving on — a half-installed fabric
will silently skip dispatches and the user won't know why.
