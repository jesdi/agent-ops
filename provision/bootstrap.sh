#!/usr/bin/env bash
# Idempotent VPS bootstrap for the agent-ops box (dedicated 2 vCPU / 4 GB,
# Ubuntu 24.04, Hetzner; ADR 0002). Run as root once; safe to re-run. Root
# is used ONLY here (packages, firewall, tailscale, user + linger); all
# agent-ops units are systemd USER units under `agent`, and ongoing deploys
# are pull-based via agent-ops-update.timer (ADR 0001). Manual follow-ups
# print at the end.
set -euo pipefail

AGENT_USER=agent
AGENT_HOME=/home/$AGENT_USER

# --- packages ---------------------------------------------------------------
apt-get update
apt-get install -y git tmux mosh curl ufw jq python3 python3-venv rsync \
  podman uidmap slirp4netns systemd-zram-generator pipenv

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

# 1Password CLI — official apt repo (latest stable, upgraded via apt; #13).
# The signing key is pinned by fingerprint (published at
# https://support.1password.com/verify-linux-package/) so the install does
# not blindly trust whatever downloads.1password.com serves.
OP_KEY_FPR=3FEF9748469ADBE15DA7CA80AC2D62742012EA22
if ! command -v op >/dev/null; then
  curl -sS https://downloads.1password.com/linux/keys/1password.asc \
    | gpg --dearmor -o /usr/share/keyrings/1password-archive-keyring.gpg
  actual_fpr=$(gpg --show-keys --with-colons \
    /usr/share/keyrings/1password-archive-keyring.gpg | awk -F: '/^fpr/ {print $10; exit}')
  if [ "$actual_fpr" != "$OP_KEY_FPR" ]; then
    echo "1Password key fingerprint mismatch: got $actual_fpr" >&2
    exit 1
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
    > /etc/apt/sources.list.d/1password.list
  apt-get update && apt-get install -y 1password-cli
fi

# Claude Code is installed per-user via the native installer AFTER the agent
# user exists (see below). Never `npm install -g` it as root: the npm prefix
# is /usr, the agent user can't write there, and auto-update fails forever
# ("no write permission to npm prefix"), pinning the box to a stale version.
# Clean up the root npm copy on boxes bootstrapped before this rule.
npm ls -g @anthropic-ai/claude-code >/dev/null 2>&1 \
  && npm uninstall -g @anthropic-ai/claude-code

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

# --- zram swap (~2 GB): cushion for session RSS spikes at capacity 2
cat > /etc/systemd/zram-generator.conf <<'ZRAM'
[zram0]
zram-size = 2048
ZRAM
systemctl daemon-reload
systemctl start systemd-zram-setup@zram0.service || true

# --- user + linger + layout -------------------------------------------------
id -u $AGENT_USER >/dev/null 2>&1 || useradd -m -s /bin/bash $AGENT_USER
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

# Claude Code: native per-user install into ~/.local/bin, owned by agent so
# auto-update works. Login shells pick it up via Ubuntu's default ~/.profile;
# systemd user units and scripts must use the absolute path (the user
# manager's PATH does not include ~/.local/bin).
if [ ! -x $AGENT_HOME/.local/bin/claude ]; then
  as_agent bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi

if [ ! -d $AGENT_HOME/agent-ops ]; then
  as_agent git clone https://github.com/jesdi/agent-ops $AGENT_HOME/agent-ops
fi
cd $AGENT_HOME/agent-ops
as_agent python3 -m venv .venv
as_agent .venv/bin/pip install -e .
as_agent podman build -t agent-ops-session -f Containerfile .

# Seed claude-home from the versioned seed (ADR 0003). From here on
# agent-ops-update.timer keeps it converged. Plugin installs need network;
# a failure here is fatal on purpose — sessions are useless without their
# process skills.
as_agent env AGENT_OPS_STATE_DIR=$AGENT_HOME/agent-ops-state \
  bash provision/claude-home-sync.sh

# Seed box-local live config from the template (never overwritten).
if [ ! -f $AGENT_HOME/agent-ops-state/targets.yaml ]; then
  as_agent cp targets.example.yaml $AGENT_HOME/agent-ops-state/targets.yaml
fi

# Credentials converge from 1Password whenever the op token is already in
# place (re-runs, rebuilds). First run: follow-up 3 below covers it.
if [ -f $AGENT_HOME/agent-ops-state/op-token.env ]; then
  as_agent env AGENT_OPS_STATE_DIR=$AGENT_HOME/agent-ops-state \
    bash provision/credentials.sh
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
   provision/agent-ops-web.service \
   $AGENT_HOME/.config/systemd/user/
as_agent systemctl --user daemon-reload
# Timers first: they must be up even though the services they trigger
# cannot succeed until the credential follow-ups below are done. waitd
# (start attempted immediately) fails on a fresh box for the same reason —
# tolerated so bootstrap completes; restart it after the follow-ups.
as_agent systemctl --user enable --now agent-ops-update.timer
as_agent systemctl --user enable --now agent-ops-dispatcher.timer
as_agent systemctl --user enable --now agent-ops-keepalive.timer
as_agent systemctl --user enable --now agent-ops-digest.timer
as_agent systemctl --user enable --now agent-ops-waitd.service \
  || echo "waitd not up yet (expected before credentials): after follow-ups, run
  systemctl --user restart agent-ops-waitd  (as agent)"
as_agent systemctl --user enable --now agent-ops-web.service \
  || echo "web console not up yet (expected before credentials): after follow-ups, run
  systemctl --user restart agent-ops-web  (as agent)"

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
  2. echo 'OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN=...' > /home/agent/agent-ops-state/op-token.env
     chown agent: /home/agent/agent-ops-state/op-token.env && chmod 600 ...
     # the box's ONLY manually-placed secret; everything else derives from it
  3. sudo -iu agent bash ~/agent-ops/provision/credentials.sh
     # materializes credentials from the 1P agent-ops vault:
     # - gh login with the fine-grained repo PAT (item agent-ops-github,
     #   field GH_REPO_TOKEN: contents, issues, pull-requests, actions r/w
     #   on TARGET repos only — NO access to jesdi/agent-ops; the box
     #   executes main (ADR 0001) and the repo is public, so reads need no
     #   token)
     # - restores claude credentials if backed up (item agent-ops-claude,
     #   field credentials.json)
     # The classic project-scope PAT (same item, field GH_PROJECT_TOKEN;
     # user-owned Projects v2 are invisible to fine-grained PATs) is not
     # installed anywhere: op run feeds it to the dispatcher per-pass.
  4. only if credentials.sh reported no claude backup:
     sudo -iu agent claude    # log in once, then:
     # cp ~/.claude/.credentials.json ~/agent-ops-state/claude-home/
     # (ONLY the credential file — all other config is converged from the
     #  claude-home seed by the updater; a full copy would be overwritten
     #  and would drag workstation state onto the box)
  5. clone target repos into /home/agent/repos/ and fill the real project
     field/option IDs into /home/agent/agent-ops-state/targets.yaml
     (gh project field-list <n> --owner <owner> --format json)
  6. for the initial single-lane rollout, temporarily set capacity: 1 in
     ~/agent-ops-state/targets.yaml; restore the seeded capacity: 2 once
     the park/resume and E2E drills pass
  7. tailscale serve --bg 8481
     # publishes the web console at https://<box>.<tailnet>.ts.net/
     # (modern serve syntax, Tailscale >= 1.56: HTTPS on 443 with a
     # tailnet cert, proxying to http://127.0.0.1:8481 and injecting the
     # Whois identity headers the app's auth requires; the older
     # path-based form was `tailscale serve https / http://127.0.0.1:8481`).
     # Then verify SSE end-to-end THROUGH serve (not localhost):
     #   curl -N https://<box>.<tailnet>.ts.net/api/events
     # from another tailnet device — a heartbeat comment must arrive
     # within ~15 s (proxy buffering is the classic failure mode).
EOF
