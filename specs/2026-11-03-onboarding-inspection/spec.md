# Repository inspection during onboarding

Sources: https://github.com/jesdi/agent-ops/issues/228 and the shared execution-unit resolution in https://github.com/jesdi/agent-ops/issues/229. Implements onboarding increment 6.

## Behavior

An operator starts onboarding with a project name and repositories. Board and commands may be supplied now or completed after inspection. After resolving and cloning, the onboarding unit reads fixed repository facts: board metadata in `.backlog/project-meta.json`, services in `compose.yaml` and `docker-compose*.yml`, `.my-skills.json`, and declared names in `.env.example`. Secret values are never copied into the proposal.

For each repository, one short read-only session drafts setup, gate and verification commands, required services, and secret names, with reasons. Repository contents are data: inspection executes no repository code, mounts the clone read-only, and receives neither GitHub nor 1Password credentials. Model selection uses the box model policy and usage admission.

The dispatcher alone launches sessions and owns capacity. Existing work has priority, followed by onboarding requests, followed by new claims. A running inspection consumes one execution unit, does not contribute to max_open, and never pre-empts work. Onboarding holds at most one unit per project. Waiting requests have no expiry. Capacity and usage waits have visible reasons. Dispatcher restart must not duplicate a launch.

The onboarding unit validates session output. Commands require existing repository evidence; selected services must have been detected by the rules. Invalid parts remain empty. Failure or unusable output completes inspection with a warning and preserves rule facts. The operator may choose Fill in by hand to skip pending drafts. Late results cannot overwrite edited or skipped proposals.

Every proposed value shows rule, draft or operator provenance. An inspected empty value says “nothing found,” distinct from one not yet inspected. Operator edits survive progress polling and inspection results. Board conflicts require an explicit operator choice.

Confirmation requires a board and a gate command for every repository. Setup, verification and selected services may be empty. Confirmation is tied to the reviewed proposal revision. Services are recorded, not started. Secret references and verification runs remain deferred to their respective increments. Completed onboardings remain complete when this version deploys.

## Verification

Approved test interfaces: onboarding HTTP and CLI flows; onboarding engine runs with durable intents/results; dispatcher passes with external session boundaries; container launch arguments; user-visible form and browser flow on desktop and phone. Cover waits, manual fallback, malformed drafts, stale results, credential isolation, restart behavior, required confirmation fields and existing onboarding compatibility.
