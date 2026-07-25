# Session image for agent-ops (built on the box by bootstrap.sh and by
# update.sh whenever this file changes). Rootless: container root maps to
# the unprivileged `agent` user on the host.
FROM node:24-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-venv jq curl ca-certificates make \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*
# uv-managed CPython: pipenv resolves python3.13 from PATH; a future
# version bump is a one-line change.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
RUN uv python install 3.13 && ln -s "$(uv python find 3.13)" /usr/local/bin/python3.13
# Current pipenv via uv (NOT bookworm's apt pipenv 2022.12.19, whose vendored
# pip predates Python 3.12 and crashes under the 3.13 interpreter with
# `pkgutil.ImpImporter` AttributeErrors on every install).
RUN UV_TOOL_BIN_DIR=/usr/local/bin uv tool install --python 3.13 pipenv
RUN npm install -g @anthropic-ai/claude-code && corepack enable pnpm
