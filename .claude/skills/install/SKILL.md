---
name: install
description: Step-by-step procedure for installing agent-fabric on a fresh Ubuntu 24.04 LTS VPS and registering the first managed project. Invoke when the user asks to "install", "deploy", "set up", "provision", or "bring up" agent-fabric on a server, VM, or VPS — or asks how to run it next to a managed project like teach-me-eng-bot. Project-internal; not rendered into managed projects by `fabric sync`.
version: 1.1.0
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

## What you'll end up with

```
/srv/agent-fabric/          # checkout + .venv          (the operator's user)
/srv/projects/<name>/       # cloned managed repos      (fabric:fabric, 0750)
/var/lib/fabric/            # FABRIC_HOME (state.db, logs/)   (fabric:fabric)
/etc/fabric/env             # systemd EnvironmentFile   (fabric:fabric, 0600)
/etc/systemd/system/fabric.service
```

One systemd unit (`fabric.service`) running as system user `fabric`,
serving REST on `127.0.0.1:7878` and the Telegram bot inside the same
process. Scheduler ticks every 60 s; one `claude -p` subprocess at a time
across all projects (single-flight invariant — see DESIGN.md).

## Prerequisites — confirm before starting

Before running anything, confirm with the user that they have:

- A fresh Ubuntu 24.04 LTS VPS with sudo / root access.
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

## Step 1 — system packages

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git curl jq ca-certificates
```

Ubuntu 24.04 ships Python 3.12 in the default repos, which matches the
`requires-python = ">=3.12"` pin in `pyproject.toml` and what CI tests
against — no PPA or deadsnakes needed.

Confirm the interpreter is on `$PATH` before continuing:

```bash
python3.12 --version       # e.g. Python 3.12.3
```

## Step 2 — install `gh` and `claude` CLIs

`gh` (system-wide):

```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod 0644 /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt-get update && sudo apt-get install -y gh
```

`claude` (Anthropic Claude Code CLI): follow
https://docs.claude.com/en/docs/claude-code — Linux install instructions
change occasionally, so re-read rather than caching a stale command. The
binary must end up on `$PATH` for the `fabric` system user (typically
`/usr/local/bin/claude` works).

Verify both:

```bash
gh --version
claude --version
```

## Step 3 — check out and install agent-fabric

```bash
sudo install -d -o "$USER" /srv/agent-fabric
git clone https://github.com/valdisd96/agent-fabric /srv/agent-fabric
cd /srv/agent-fabric
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
fabric --help            # sanity check — must list 10 subcommands
deactivate
```

If `pip install` errors with `Package requires a different Python:
3.x is not in '>=3.12'`, the venv was created with the wrong interpreter
— delete `.venv/` and re-run with `python3.12 -m venv` explicitly.

## Step 4 — install the systemd unit (creates the `fabric` user)

```bash
sudo bash /srv/agent-fabric/scripts/install-systemd.sh
```

The script is idempotent. It creates:

- system user `fabric` with `$HOME=/var/lib/fabric`, shell `/usr/sbin/nologin`
- `/etc/fabric/env` with stub values, mode 0600, owned by `fabric`
- `/etc/systemd/system/fabric.service` with the hardening flags
  (`ProtectSystem=full`, `ProtectHome=true`, `PrivateTmp=true`,
  `NoNewPrivileges=true`, `ReadWritePaths=$FABRIC_HOME`)

**Do not start the unit yet** — auth comes first, otherwise the service
will boot and fail to dispatch because `gh`/`claude` aren't logged in.

## Step 5 — authenticate `gh` and `claude` **as the `fabric` user**

This is the most-missed gotcha. The service runs as `fabric`, so
credentials must live in `/var/lib/fabric/.config/gh/` and
`/var/lib/fabric/.claude/` — not in your sudo user's home.

`nologin` does **not** block `sudo -u fabric -H bash -c '…'` — sudo
invokes the requested binary directly, ignoring the user's login shell.
No `usermod` flip is needed.

```bash
# gh — paste the PAT when prompted
sudo -u fabric -H bash -c 'gh auth login --hostname github.com'

# claude — interactive OAuth; see "Headless claude login" below if no browser
sudo -u fabric -H bash -c 'claude login'
```

Verify:

```bash
sudo -u fabric -H bash -c 'gh auth status'
sudo -u fabric -H bash -c 'claude --version && ls -la ~/.claude'
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
sudo -e /etc/fabric/env       # opens with sudo's default editor
```

Set the Telegram pair (omit both for REST-only mode):

```bash
FABRIC_HOME=/var/lib/fabric
FABRIC_HOST=127.0.0.1
FABRIC_PORT=7878
FABRIC_TELEGRAM_TOKEN=<from BotFather, looks like 1234567890:AA…>
FABRIC_TELEGRAM_CHAT_ID=<numeric, from @userinfobot>
```

File must end up `fabric:fabric, 0600`. If the editor changed ownership:

```bash
sudo chown fabric:fabric /etc/fabric/env && sudo chmod 0600 /etc/fabric/env
```

## Step 7 — start the service

```bash
sudo systemctl start fabric
sudo systemctl status fabric --no-pager     # expect: active (running)
sudo journalctl -u fabric -f                # tick loop should log within 60s
```

If `status` shows `failed`, dump the last 50 lines and inspect:

```bash
sudo journalctl -u fabric -n 50 --no-pager
```

Common first-boot failures:

| Log signature | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: fabric` | venv at wrong path | confirm `/srv/agent-fabric/.venv/bin/fabric` exists |
| `gh: command not found` (in dispatch logs) | `gh` not on `fabric`'s `$PATH` | `sudo ln -s "$(command -v gh)" /usr/local/bin/gh` |
| `OperationalError: unable to open database file` | `$FABRIC_HOME` permissions wrong | `sudo chown -R fabric:fabric /var/lib/fabric` |
| `telegram error: Unauthorized` | bad bot token | re-check `FABRIC_TELEGRAM_TOKEN`, restart |
| One-line warning about TG creds, then REST-only | both TG vars unset | expected; fill them and `systemctl restart fabric` |

## Step 8 — register the first managed project

`/srv/projects` is **not** created by the installer; create it
`fabric`-owned first:

```bash
sudo install -d -o fabric -g fabric -m 0750 /srv/projects
```

Then clone, configure, register, label, and sync — all as `fabric`:

**Critical:** `sudo -u` does *not* load `/etc/fabric/env`, so `FABRIC_HOME`
is unset by default and the CLI falls back to `~/.fabric/projects.yaml`
— a path the systemd service does not read. Export it explicitly inside
every `sudo -u fabric` block. As of fabric 0.1.x, `fabric` itself emits
a stderr warning when this divergence is detected, so you'll notice
quickly if the export is missed.

```bash
sudo -u fabric -H bash <<'EOF'
set -euo pipefail
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
"$FABRIC" sync <project-name>                     # render skills into .claude/skills/
EOF
```

Substitute `<owner>/<repo>` and `<project-name>` (the `project.name` from
`config.yaml`). After `sync`, the rendered skill files are *uncommitted*
in the project working tree — guide the user to commit them upstream:

```bash
sudo -u fabric -H bash -c 'cd /srv/projects/<repo> && git status'
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
sudo journalctl -u fabric -f
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric logs <project> <issue#> --follow
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
sudo systemctl restart fabric
sudo systemctl stop fabric
sudo systemctl status fabric --no-pager

# Logs
sudo journalctl -u fabric -f                       # service stdout/stderr
sudo ls /var/lib/fabric/logs/                      # per-dispatch claude -p logs
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric logs <project> <n> --follow

# State (read-only is safe while running)
sudo -u fabric sqlite3 -readonly /var/lib/fabric/state.db '.tables'

# CLI escape hatches (always as fabric)
F=/srv/agent-fabric/.venv/bin/fabric
sudo -u fabric "$F" pause --reason "demo"
sudo -u fabric "$F" resume
sudo -u fabric "$F" dispatch <project> <issue#> plan-exec
sudo -u fabric "$F" sync <project> --check         # drift check
```

## Upgrading

```bash
cd /srv/agent-fabric
sudo -u fabric git pull --ff-only
sudo /srv/agent-fabric/.venv/bin/pip install -e .
sudo systemctl restart fabric

# Re-render skills if templates moved (per managed project)
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric sync <project>
```

If a template's `fabric_version` bumped major and a managed project's
`.fabric/config.yaml` still pins the old major, `sync` errors with a
version mismatch — bump the pin in the project's YAML and re-sync.

## Hand-off checklist

Before declaring the install complete, verify all of:

- [ ] `systemctl is-active fabric` → `active`
- [ ] `journalctl -u fabric --since "1 minute ago"` shows scheduler tick
- [ ] `sudo -u fabric gh auth status` → logged in
- [ ] `sudo -u fabric claude --version` → no auth errors
- [ ] At least one project shows up in `fabric list` (run as fabric)
- [ ] `setup-labels <project> --check` → "labels are clean"
- [ ] `sync <project> --check` → exit 0
- [ ] Telegram `/status` responds (if TG configured)
- [ ] Smoke issue cycled through all four states and merged

If any item fails, debug it before moving on — a half-installed fabric
will silently skip dispatches and the user won't know why.
