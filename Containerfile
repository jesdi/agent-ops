# Session image for agent-ops (built on the box by bootstrap.sh and by
# update.sh whenever this file changes). Rootless: container root maps to
# the unprivileged `agent` user on the host.
FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-venv pipenv jq curl ca-certificates make \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code && corepack enable pnpm
