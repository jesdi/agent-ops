# Isolate fixture Git from session guards

## Problem Statement
BOX sessions cannot pass the unchanged backend suite because production Git guards reject local fixture pushes to main and deletion of disposable branches. Tasks 43 and 77 remain parked. This is agent-ops issue 284, related to infra issue 106.

## Solution
Pytest routes Git commands for each test's disposable repositories to system Git while commands targeting other repositories retain the original Git executable and its guardrails. No test is skipped or mocked.

## User Stories
1. As an operator, I want full gates to run in BOX sessions so completed tickets can progress.
2. As a test author, I want real Git fixture behavior independent of the session's production policy.
3. As an operator, I want shared task worktrees to keep their guards throughout test execution.

## Implementation Decisions
Use a pytest fixture to prepend a test-only dispatcher to PATH only when the selected Git is the BOX guard. Resolve Git's common metadata directory with the supplied global options; choose system Git only when that directory is inside the current test's temporary directory. Unknown or failed probes fall back to the original guard. A linked worktree inside the fixture whose common repository is elsewhere keeps the guard. The setting exists only in the test process environment.

## Testing Decisions
Existing seam: real Git commands launched in subprocesses. New seam: the test-only Git dispatcher executable. Exercise local main pushes and forced branch deletion with real temporary repositories; verify outside repositories and linked worktrees still hit a recording guard. Run existing workspace/token fixture tests and the full backend suite. Reproduce in BOX's current session image before and after. Frontend unchanged.

## Out of Scope
Changing production Git policy, skipping tests, force-pushing task branches, or installing missing sudo (infra issue 105).

## Further Notes
Tasks need this fixture change in their branch before rerunning the full gate. The separate infra dependency fix supplies pr-risk and sudo.
