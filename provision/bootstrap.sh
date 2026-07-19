#!/usr/bin/env bash
# Idempotent VPS bootstrap for the agent-ops box (Ubuntu 24.04, Hetzner).
# Run as root once; safe to re-run. Root is used ONLY here (packages,
# firewall, tailscale, user + linger); all agent-ops units are systemd
# USER units under `agent`, and ongoing deploys are pull-based via
# agent-ops-update.timer (ADR 0001). Manual follow-ups print at the end.
set -euo pipefail

AGENT_USER=agent
AGENT_HOME=/home/$AGENT_USER

# --- packages ---------------------------------------------------------------
apt-get update
apt-get install -y git tmux mosh curl ufw jq python3 python3-venv \
  docker.io docker-compose-v2 pipenv

# node + pnpm (via corepack)
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
corepack enable pnpm || true

# gh CLI
if ! command -v gh >/dev/null; then
  mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update && apt-get install -y gh
fi

# 1Password CLI
if ! command -v op >/dev/null; then
  curl -sSfo /tmp/op.deb \
    "https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.0/op_linux_$(dpkg --print-architecture)_v2.30.0.deb" \
    && apt-get install -y /tmp/op.deb
fi

# Claude Code
command -v claude >/dev/null || npm install -g @anthropic-ai/claude-code

# Tailscale
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# --- firewall: Tailscale-only ingress ---------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0
ufw allow 41641/udp   # tailscale
ufw --force enable

# --- user + linger + layout -------------------------------------------------
id -u $AGENT_USER >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker $AGENT_USER
loginctl enable-linger $AGENT_USER

AGENT_UID=$(id -u $AGENT_USER)
# Wait for the lingering user manager to come up.
for _ in $(seq 1 30); do
  [ -S /run/user/$AGENT_UID/bus ] && break
  sleep 1
done
[ -S /run/user/$AGENT_UID/bus ] || { echo "user manager for $AGENT_USER never started" >&2; exit 1; }

as_agent() {
  sudo -u $AGENT_USER XDG_RUNTIME_DIR=/run/user/$AGENT_UID \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$AGENT_UID/bus "$@"
}

as_agent mkdir -p \
  $AGENT_HOME/repos \
  $AGENT_HOME/agent-ops-state \
  $AGENT_HOME/.config/systemd/user

if [ ! -d $AGENT_HOME/agent-ops ]; then
  as_agent git clone https://github.com/jesdi/agent-ops $AGENT_HOME/agent-ops
fi
cd $AGENT_HOME/agent-ops
as_agent python3 -m venv .venv
as_agent .venv/bin/pip install -e .

# Seed box-local live config from the template (never overwritten).
if [ ! -f $AGENT_HOME/agent-ops-state/targets.yaml ]; then
  as_agent cp targets.example.yaml $AGENT_HOME/agent-ops-state/targets.yaml
fi

# --- systemd user units ------------------------------------------------------
# Initial install; from here on agent-ops-update.timer keeps them in sync.
as_agent cp provision/agent-ops-dispatcher.service \
   provision/agent-ops-dispatcher.timer \
   provision/agent-ops-waitd.service \
   provision/agent-ops-keepalive.service \
   provision/agent-ops-keepalive.timer \
   provision/agent-ops-digest.service \
   provision/agent-ops-digest.timer \
   provision/agent-ops-update.service \
   provision/agent-ops-update.timer \
   $AGENT_HOME/.config/systemd/user/
as_agent systemctl --user daemon-reload
as_agent systemctl --user enable --now agent-ops-waitd.service
as_agent systemctl --user enable --now agent-ops-dispatcher.timer
as_agent systemctl --user enable --now agent-ops-keepalive.timer
as_agent systemctl --user enable --now agent-ops-digest.timer
as_agent systemctl --user enable --now agent-ops-update.timer

cat <<'EOF'
bootstrap done. Manual follow-ups (interactive, once):

  *** FIREWALL LOCKOUT WARNING ***
  ufw is now ACTIVE (deny all incoming except tailscale0 and UDP 41641).
  SSH over the public IP is already BLOCKED. If you are connected via SSH
  over the public IP and your session ends before Tailscale is up, you will
  be locked out — recoverable only via your provider's out-of-band console.
  ALWAYS run this script inside tmux or mosh. Complete step 1 (tailscale up)
  and confirm `tailscale ssh <host>` connectivity BEFORE closing this session.
  ***

  1. tailscale up
  2. sudo -iu agent claude    # login with the subscription account
  3. sudo -iu agent gh auth login   # fine-grained PAT: issues, projects,
     contents, pull-requests on TARGET repos only — the PAT must NOT have
     write access to jesdi/agent-ops (the box executes main; ADR 0001)
  4. echo 'OP_SERVICE_ACCOUNT_TOKEN=...' > /home/agent/agent-ops-state/op-token.env
     chown agent: /home/agent/agent-ops-state/op-token.env && chmod 600 ...
  5. clone target repos into /home/agent/repos/ and fill the real project
     field/option IDs into /home/agent/agent-ops-state/targets.yaml
     (gh project field-list <n> --owner <owner> --format json)
  6. start with capacity: 1 in ~/agent-ops-state/targets.yaml for the
     single-lane rollout
EOF
