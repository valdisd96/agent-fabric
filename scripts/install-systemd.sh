#!/usr/bin/env bash
#
# install-systemd.sh — idempotent installer for the agent-fabric systemd unit.
# Targets a generic Linux VPC VM (Debian/Ubuntu tested). Replaces the earlier
# Pi-on-Tailscale plan; see DESIGN.md "Decision 13 — Deployment".
#
# Usage:
#   sudo bash scripts/install-systemd.sh
#
# Environment overrides (optional):
#   FABRIC_USER         system user to run the service as           [fabric]
#   FABRIC_HOME         FABRIC_HOME / state.db location             [/var/lib/fabric]
#   INSTALL_PREFIX      where the agent-fabric checkout lives       [/srv/agent-fabric]

set -euo pipefail

FABRIC_USER=${FABRIC_USER:-fabric}
FABRIC_HOME=${FABRIC_HOME:-/var/lib/fabric}
INSTALL_PREFIX=${INSTALL_PREFIX:-/srv/agent-fabric}
PROJECTS_DIR=${PROJECTS_DIR:-/srv/projects}

UNIT_PATH=/etc/systemd/system/fabric.service
ENV_DIR=/etc/fabric
ENV_FILE=$ENV_DIR/env
VENV_PYTHON=$INSTALL_PREFIX/.venv/bin/fabric

log() { echo "[install-systemd] $*"; }
warn() { echo "[install-systemd] WARN: $*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "must run as root (sudo bash $0)" >&2
    exit 1
  fi
}

ensure_user() {
  if id -u "$FABRIC_USER" >/dev/null 2>&1; then
    log "user $FABRIC_USER exists"
  else
    log "creating system user $FABRIC_USER"
    useradd --system --home "$FABRIC_HOME" --create-home --shell /usr/sbin/nologin "$FABRIC_USER"
  fi
  install -d -o "$FABRIC_USER" -g "$FABRIC_USER" -m 0750 "$FABRIC_HOME"
}

ensure_projects_dir() {
  if [[ -d "$PROJECTS_DIR" ]]; then
    log "$PROJECTS_DIR exists"
  else
    log "creating $PROJECTS_DIR ($FABRIC_USER:$FABRIC_USER 0750)"
  fi
  install -d -o "$FABRIC_USER" -g "$FABRIC_USER" -m 0750 "$PROJECTS_DIR"
}

# `claude` installed via the official curl|sh installer lands at
# ~/.local/share/claude/versions/<v> — under HOME, which the unit's
# ProtectHome=true makes invisible to the service. A naive symlink from
# /usr/local/bin/claude resolves to that hidden path and every dispatch
# fails at exec. Detect and tell the operator how to relocate.
check_claude_path() {
  local claude_path
  claude_path=$(command -v claude 2>/dev/null) || {
    warn "'claude' not on PATH. Install Claude Code (https://docs.claude.com) before starting the service."
    return 0
  }
  local resolved
  resolved=$(readlink -f "$claude_path")
  case "$resolved" in
    /root/*|/home/*)
      warn "claude resolves to $resolved (under \$HOME)."
      warn "ProtectHome=true in fabric.service will hide this path from the service."
      warn "Fix: copy the binary out of \$HOME and re-symlink, e.g.:"
      warn "  install -d /usr/local/share/claude"
      warn "  cp $resolved /usr/local/share/claude/claude-\$(claude --version | awk '{print \$1}')"
      warn "  ln -sfn /usr/local/share/claude/claude-* /usr/local/bin/claude"
      ;;
    *)
      log "claude resolves to $resolved (OK — outside \$HOME)"
      ;;
  esac
}

ensure_env_file() {
  install -d -m 0755 "$ENV_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
# /etc/fabric/env — sourced by systemd. Mode 0600, owner $FABRIC_USER.
FABRIC_HOME=$FABRIC_HOME
FABRIC_HOST=127.0.0.1
FABRIC_PORT=7878

# Telegram bot — fill both to enable. Comment out for REST-only.
# FABRIC_TELEGRAM_TOKEN=
# FABRIC_TELEGRAM_CHAT_ID=
EOF
    log "created $ENV_FILE — fill TG creds then: systemctl restart fabric"
  else
    log "env file $ENV_FILE present, leaving as-is"
  fi
  chown "$FABRIC_USER:$FABRIC_USER" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
}

install_unit() {
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Agent Fabric
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$FABRIC_USER
WorkingDirectory=$INSTALL_PREFIX
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_PYTHON server
Restart=always
RestartSec=5
# Hardening: read-only filesystem except FABRIC_HOME and the temp dir.
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=$FABRIC_HOME

[Install]
WantedBy=multi-user.target
EOF
  log "wrote $UNIT_PATH"
}

main() {
  require_root
  ensure_user
  ensure_projects_dir
  ensure_env_file
  install_unit
  check_claude_path
  systemctl daemon-reload
  systemctl enable fabric
  log "installed. Edit $ENV_FILE and run: systemctl start fabric"
}

main "$@"
