# Runbook: GitHub credentials on the box

How box sessions authenticate to GitHub, what each secret in the 1Password
`agent-ops` vault is for, and how to diagnose the two failure modes that
have actually occurred. Written after issue jesdi/portfolio_eval#133 cost
two park/resume round-trips to rediscover all of this (2026-08-02/03).

## The credential model

There is exactly **one** GitHub auth path on the box, and it is HTTPS:

- A **fine-grained PAT** (1P item `agent-ops-github`, field
  `GH_REPO_TOKEN`) is installed into `gh`'s credential store by
  `provision/credentials.sh`. Git reaches it through the
  `!gh auth git-credential` helper in the global gitconfig.
- Session containers mount only `~/.config/gh` and `~/.gitconfig`
  (read-only; see `dispatcher/containers.py`). There is **no `~/.ssh`
  and no ssh-agent inside sessions** — an SSH auth path does not exist,
  and all target-repo remotes are `https://`.
- The classic project-scope PAT (same item, field `GH_PROJECT_TOKEN`)
  is never installed anywhere; `op run` feeds it to the dispatcher
  per-pass for Projects v2 board access only.

### The SSH key is signing-only

The SSH key in 1P item `agent-ops-git-signing` is a **commit signing**
key, nothing more. `credentials.sh` restores it to
`$STATE_DIR/git-signing-key` and wires it into `gpg.format ssh` /
`user.signingkey` so commits show as Verified. It is deliberately **not**
an authentication credential:

- Adding it to GitHub (as either a signing or an auth key) does not and
  cannot affect pushes — sessions never speak SSH.
- If a push fails, rotating/re-adding this key is never the fix.

## Failure mode 1: workflow-file pushes

**Symptom** — a push is rejected with:

    ! [remote rejected] ... (refusing to allow a Personal Access Token to
    create or update workflow .github/workflows/<file> without workflow scope)

GitHub refuses pushes that create or modify anything under
`.github/workflows/` unless the PAT carries the workflow permission —
even when the token otherwise has full push rights. The session parks with
this error in its park note.

**Fix** — edit the fine-grained PAT (github.com → Settings → Developer
settings → Fine-grained tokens) and add **Workflows: Read and write** for
the target repo. Permission edits apply **live to the existing token**:

- No token rotation needed; nothing on the box changes.
- No `credentials.sh` re-run needed (only re-run it if the token *value*
  was regenerated, after updating `GH_REPO_TOKEN` in 1P).
- Resume the parked task; the plain `git push` retry succeeds.

There is no read-only API probe for this capability — only a real push
proves it. Keeping the permission granted on target repos avoids the
round-trip entirely; CI-hardening issues legitimately edit workflow files.

## Failure mode 2: "no credentials found" is usually mis-scoping

When a session reports it cannot find or use GitHub credentials, check in
this order (all from the host):

1. `gh auth status` — is the PAT installed and active?
2. `gh api repos/<owner>/<repo> -q .permissions` — does the PAT cover
   *this* repo? The fine-grained PAT is scoped to an explicit repo list;
   a new target repo must be added to it.
3. The exact git error in the park note / `events.jsonl` — a
   workflow-scope refusal (failure mode 1) is not a missing credential,
   and neither is a `publickey` error (the session tried SSH, which by
   design cannot work — see above).

Re-running `bash ~/agent-ops/provision/credentials.sh` is idempotent and
safe whenever the 1P items changed.
