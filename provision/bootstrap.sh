#!/usr/bin/env bash
# Idempotent VPS bootstrap for the agent-ops box (Ubuntu 24.04, Hetzner).
# Run as root once; safe to re-run. Manual follow-ups are printed at the end.
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

# --- user + layout ----------------------------------------------------------
id -u $AGENT_USER >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker $AGENT_USER
sudo -u $AGENT_USER mkdir -p \
  $AGENT_HOME/repos \
  $AGENT_HOME/agent-ops-state

if [ ! -d $AGENT_HOME/agent-ops ]; then
  sudo -u $AGENT_USER git clone https://github.com/jesdi/agent-ops \
    $AGENT_HOME/agent-ops
fi
cd $AGENT_HOME/agent-ops
sudo -u $AGENT_USER python3 -m venv .venv
sudo -u $AGENT_USER .venv/bin/pip install -e .

# --- systemd ----------------------------------------------------------------
cp provision/agent-ops-dispatcher.service \
   provision/agent-ops-dispatcher.timer \
   provision/agent-ops-waitd.service \
   provision/agent-ops-keepalive.service \
   provision/agent-ops-keepalive.timer \
   provision/agent-ops-digest.service \
   provision/agent-ops-digest.timer \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agent-ops-waitd.service
systemctl enable --now agent-ops-dispatcher.timer
systemctl enable --now agent-ops-keepalive.timer
systemctl enable --now agent-ops-digest.timer

cat <<'EOF'
bootstrap done. Manual follow-ups (interactive, once):
  1. tailscale up
  2. sudo -iu agent claude    # login with the subscription account
  3. sudo -iu agent gh auth login   # fine-grained PAT: issues, projects,
     contents, pull-requests on target repos only
  4. echo 'OP_SERVICE_ACCOUNT_TOKEN=...' > /home/agent/agent-ops-state/op-token.env
     chown agent: /home/agent/agent-ops-state/op-token.env && chmod 600 ...
  5. clone target repos into /home/agent/repos/ and fill the real project
     field/option IDs into /home/agent/agent-ops/targets.yaml
     (gh project field-list <n> --owner <owner> --format json)
  6. start with capacity: 1 in targets.yaml for the single-lane rollout
EOF
