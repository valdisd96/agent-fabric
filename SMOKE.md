# SMOKE — first end-to-end run on a VPC VM

End-to-end walkthrough for bringing up `agent-fabric` on a fresh Linux VM
and watching one full `state:needs-planning → merged` cycle land via
Telegram. This replaces the earlier Pi/Tailscale plan — see DESIGN.md
"Decision 13 — Deployment" for the rationale.

## Prerequisites

- Ubuntu 24.04 (or Debian 12) VPC VM with sudo access.
- `gh` CLI installed and a Personal Access Token with `repo` scope.
- `claude` CLI installed (Anthropic's Claude Code).
- A Telegram bot token from `@BotFather` and your numeric `chat_id`
  (easiest: message [@userinfobot](https://t.me/userinfobot)).

## 1. Provision the VM

```bash
# As root
apt-get update && apt-get install -y python3.11 python3.11-venv git curl jq

# Install gh and claude per their docs
# (gh: https://cli.github.com — claude: https://docs.claude.com/en/docs/claude-code)
```

## 2. Check out + install agent-fabric

```bash
sudo install -d -o "$USER" /srv/agent-fabric
git clone https://github.com/valdisd96/agent-fabric /srv/agent-fabric
cd /srv/agent-fabric
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Confirm CLI imports
fabric --help
```

## 3. Authenticate

```bash
gh auth login          # paste the PAT
claude login           # see "Headless claude login" below if no browser
```

### Headless `claude login`

If the VM has no browser:

1. Run `claude login` and copy the OAuth URL it prints.
2. From your laptop: `ssh -L 8000:localhost:8000 vm-host` (or whichever
   port `claude` waits on — printed in its log).
3. Open the URL on your laptop. The OAuth callback hits `localhost:8000`
   on the laptop, which the SSH tunnel forwards to the VM's local
   listener.
4. `claude` prints "logged in"; the SSH tunnel can close.

## 4. Install the systemd unit

```bash
sudo bash scripts/install-systemd.sh
```

Edit `/etc/fabric/env`:

```bash
FABRIC_HOME=/var/lib/fabric
FABRIC_HOST=127.0.0.1
FABRIC_PORT=7878
FABRIC_TELEGRAM_TOKEN=<from BotFather>
FABRIC_TELEGRAM_CHAT_ID=<your numeric chat_id>
```

```bash
sudo systemctl start fabric
sudo systemctl status fabric    # should be "active (running)"
```

## 5. Register your first project

```bash
sudo -u fabric -i bash -lc '
  cd /srv/projects
  git clone https://github.com/valdisd96/teach-me-eng-bot
  cp /srv/agent-fabric/examples/teach-me-eng-bot.config.yaml \
     teach-me-eng-bot/.fabric/config.yaml
  /srv/agent-fabric/.venv/bin/fabric register /srv/projects/teach-me-eng-bot
  /srv/agent-fabric/.venv/bin/fabric setup-labels teach-me-eng-bot --check
  /srv/agent-fabric/.venv/bin/fabric setup-labels teach-me-eng-bot
  /srv/agent-fabric/.venv/bin/fabric sync teach-me-eng-bot
'
```

## 6. File a smoke issue

On GitHub, create an issue in `valdisd96/teach-me-eng-bot`:

- Title: `smoke: rename README heading`
- Body: ``Change `# Teach me eng bot` to `# Teach Me — English Bot` in `README.md`.``
- Labels: `state:needs-planning`, `priority:low`, `type:docs`, `area:bot`

Within 60 seconds the scheduler picks it up.

## 7. Watch the cycle

| Where | What you see |
|---|---|
| `journalctl -u fabric -f` | scheduler tick + dispatch lifecycle |
| `fabric logs teach-me-eng-bot <n> --follow` | streaming `claude -p` stdout |
| `/queue` in Telegram | the cross-project queue |
| `/status` in Telegram | paused?, project counts |

Expected progression:

1. **`state:needs-planning` → `state:in-progress`** — `plan-exec` dispatches.
2. **`state:in-progress` → `state:tests-pending`** — plan committed, branch pushed.
3. **`state:tests-pending` → `state:in-review`** — `test-writer` runs, PR opens.
4. Telegram: notification with `Approve` / `Request changes` buttons.
5. Tap `Approve` → PR merges via squash, branch deletes, issue auto-closes.

## Gotchas observed

(Filled in after the first real run.)

- `claude login` SSH-tunnel quirks:
- `gh` PAT scope:
- systemd hardening blocking writes outside `$FABRIC_HOME`:
- Log rotation under `$FABRIC_HOME/logs/`:
- TG bot `chat_id` discovery (vs username):

## Useful one-liners

```bash
# Tail the active dispatch's logs from your laptop
ssh vm "journalctl -u fabric -f"

# Force-dispatch a specific stage (debug)
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric dispatch \
    teach-me-eng-bot 42 plan-exec

# Pause the fabric without stopping the unit
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric pause --reason "demo"
```
