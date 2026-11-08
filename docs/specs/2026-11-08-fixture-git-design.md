# Run isolated gate fixtures in session images

## Problem Statement
BOX sessions cannot pass the unchanged backend suite because production Git guards reject local fixture pushes to main and deletion of disposable branches, and socket permission probes require missing sudo. Tasks 43 and 77 remain parked. This is agent-ops issue 284, related to infra issues 105 and 106.

## Solution
Pytest routes Git commands for each test's disposable repositories to system Git while commands targeting other repositories retain the original Git executable and its guardrails. No Git test is skipped or mocked. Socket permission probes drop their child UID and groups through Python, without requiring sudo.

## User Stories
1. As an operator, I want full gates to run in BOX sessions so completed tickets can progress.
2. As a test author, I want real Git fixture behavior independent of the session's production policy.
3. As an operator, I want shared task worktrees to keep their guards throughout test execution.
4. As an operator, I want the socket permission probes to run in the existing image without installing sudo.

## Implementation Decisions
Use a pytest fixture to prepend a test-only dispatcher to PATH only when the selected Git is the BOX guard. Resolve Git's common metadata directory with the supplied global options; choose system Git only when that directory is inside the current test's temporary directory. Unknown or failed probes fall back to the original guard. A linked worktree inside the fixture whose common repository is elsewhere keeps the guard. The setting exists only in the test process environment.

## Testing Decisions
Existing seam: real Git commands launched in subprocesses. New seam: the test-only Git dispatcher executable. Exercise local main pushes and forced branch deletion with real temporary repositories; verify outside repositories and linked worktrees still hit a recording guard. Run existing workspace/token fixture tests and the full backend suite. Reproduce in BOX's current session image before and after. Frontend unchanged.

## Out of Scope
Changing production Git policy, skipping tests, force-pushing task branches, or installing sudo (the operator explicitly rejected that package).

## Further Notes
Tasks need this fixture change in their branch before rerunning the full gate. The separate infra dependency fix supplies pr-risk only. For infra issue 105, the socket probe uses subprocess user/group/extra_groups with the system interpreter. Namespace root runs both a nonmember denial probe and a member success probe, adding the configured group to the latter child only; nonprivileged hosts retain capability-based skips. Root probe failures are assertions, never skipped. Validate both probes in the unchanged rootless session image where sudo is absent.
