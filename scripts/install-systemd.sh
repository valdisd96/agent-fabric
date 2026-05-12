#!/usr/bin/env bash
#
# install-systemd.sh — idempotent installer for the agent-fabric systemd unit.
# Targets a generic Linux VPC VM (Debian/Ubuntu tested). The service runs as
# root with IS_SANDBOX=1; intended for single-tenant VMs or isolated one-time
# containers where there is nothing else on the host worth isolating from.
# See DESIGN.md "Decision 13 — Deployment".
#
# Usage:
#   sudo bash scripts/install-systemd.sh
#
# Environment overrides (optional):
#   FABRIC_HOME         FABRIC_HOME / state.db location             [/var/lib/fabric]
#   INSTALL_PREFIX      where the agent-fabric checkout lives       [/srv/agent-fabric]
#   PROJECTS_DIR        where managed-repo clones land              [/srv/projects]

set -euo pipefail

FABRIC_HOME=${FABRIC_HOME:-/var/lib/fabric}
INSTALL_PREFIX=${INSTALL_PREFIX:-/srv/agent-fabric}
PROJECTS_DIR=${PROJECTS_DIR:-/srv/projects}

UNIT_PATH=/etc/systemd/system/fabric.service
ENV_DIR=/etc/fabric
ENV_FILE=$ENV_DIR/env
VENV_FABRIC=$INSTALL_PREFIX/.venv/bin/fabric

log() { echo "[install-systemd] $*"; }
warn() { echo "[install-systemd] WARN: $*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "must run as root (sudo bash $0)" >&2
    exit 1
  fi
}

ensure_dirs() {
  install -d -o root -g root -m 0750 "$FABRIC_HOME"
  install -d -o root -g root -m 0750 "$PROJECTS_DIR"
  log "ensured $FABRIC_HOME and $PROJECTS_DIR (root:root 0750)"
}

check_claude_path() {
  if ! command -v claude >/dev/null 2>&1; then
    warn "'claude' not on PATH. Install Claude Code (https://docs.claude.com) before starting the service."
    return 0
  fi
  log "claude resolves to $(readlink -f "$(command -v claude)")"
}

ensure_env_file() {
  install -d -m 0755 "$ENV_DIR"
  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
# /etc/fabric/env — sourced by systemd. Mode 0600, owner root.
FABRIC_HOME=$FABRIC_HOME
FABRIC_HOST=127.0.0.1
FABRIC_PORT=7878

# Claude Code refuses to run as root unless this is set. The service runs as
# root and we accept that on single-tenant VMs / isolated containers.
IS_SANDBOX=1

# Telegram bot — fill both to enable. Comment out for REST-only.
# FABRIC_TELEGRAM_TOKEN=
# FABRIC_TELEGRAM_CHAT_ID=
EOF
    log "created $ENV_FILE — fill TG creds then: systemctl restart fabric"
  else
    log "env file $ENV_FILE present, leaving as-is"
    if ! grep -q '^IS_SANDBOX=' "$ENV_FILE"; then
      echo "IS_SANDBOX=1" >> "$ENV_FILE"
      log "appended IS_SANDBOX=1 to $ENV_FILE (required when running claude as root)"
    fi
  fi
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
}

install_unit() {
  # ProtectHome is intentionally OFF: gh + claude credentials live under
  # /root/.config/gh and /root/.claude respectively, and the unit needs to
  # read them. The other hardening flags are kept where they're meaningful
  # even for a root-uid process.
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Agent Fabric
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_PREFIX
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_FABRIC server
Restart=always
RestartSec=5
ProtectSystem=full
PrivateTmp=true
ReadWritePaths=$FABRIC_HOME

[Install]
WantedBy=multi-user.target
EOF
  log "wrote $UNIT_PATH"
}

main() {
  require_root
  ensure_dirs
  ensure_env_file
  install_unit
  check_claude_path
  systemctl daemon-reload
  systemctl enable fabric
  log "installed. Edit $ENV_FILE and run: systemctl start fabric"
}

main "$@"
