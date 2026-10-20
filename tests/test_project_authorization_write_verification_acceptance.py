"""Acceptance tests for ticket 17 (project-onboarding): repository
authorization and required-write verification, driven through the PROJECT_CLI
seam (`agent-ops project add` to resume, `agent-ops project status`).

Spec: specs/project-onboarding/spec.md S14, S20, S21, S60 (R7, R11, R12).
Ticket: .agent/tickets/17-authorization-write-verification.md.

Interface these tests define on top of tests/test_project_cli_proposal_acceptance.py
(the ticket leaves its shape open). `project status <name>` is a read:
- `box.status(name) -> dict | None` (None = unknown project):
  `{"repos": {repo: {"authorization": {...}, "verification": {...}}},
    "capabilities": {name: {"documented": bool, "effective": bool | None,
                            "input": "present" | "missing"}},
    "identity": {"verification": str, "task": str, "task_env": [str]}}`
  - `authorization`: `{"state": "granted" | "awaiting_admin" | "unsupported",
    "action": str}`; `action` is the step the operator or an organization
    administrator must take (empty when granted).
  - `verification`: `{"state": "passed" | "failed" | "pending",
    "denied": [str], "receipts": [{"kind", "id"}],
    "cleanup": [{"id", "state": "removed" | "closed" | "failed", "detail"}]}`;
    `denied` names the refused operations; `receipts` identify the exact
    temporary resources created; `cleanup` is one entry per receipt.
- Output sections: `Authorization:`, `Write verification:`, `Cleanup:`,
  `Capabilities:` (when declared), then one line `Onboarding: complete` or
  `Onboarding: incomplete`. `complete` only when EVERY member repository is
  granted and passed. Each repository appears on its own line of its sections.
  A failed verification line carries the denied operations and the repository.
- The project board write is required evidence too: `complete` also needs the
  capability `BOARD_WRITE` to be declared effective with its input present.
  An absent or not effective board write keeps onboarding incomplete.
- A verification identity equal to the task identity, or any App key, board or
  infrastructure credential name among `task_env`, is a violation: exit code
  non-zero, stderr names it, never `complete`.
- Exit code 0 for a rendered status (complete or not); `status` never calls
  `confirm` or `resume`.
- Resuming is `project add <name> <repos>` for the registered project: it calls
  `box.resume(name)` and nothing else.
"""
import io
from copy import deepcopy

FRONT, BACK = "example/shop-frontend", "example/shop-backend"
REPO = "example/analytics"
BOARD_WRITE = "update project board"
BOARD_OK = {BOARD_WRITE: dict(documented=True, effective=True, input="present")}


def granted():
    return dict(state="granted", action="")


def passed(*ids):
    receipts = [dict(kind="branch", id="agent-ops/verify-1")] + [
        dict(kind="pr", id=i) for i in ids]
    return dict(state="passed", denied=[], receipts=receipts,
                cleanup=[dict(id=r["id"], state="closed" if r["kind"] == "pr" else "removed",
                              detail="") for r in receipts])


def failed(denied, receipts=()):
    return dict(state="failed", denied=list(denied), receipts=list(receipts), cleanup=[])


def repo_state(authorization=None, verification=None):
    return dict(authorization=authorization or granted(),
                verification=verification or passed("pr-9"))


def status(repos, capabilities=None, identity=None):
    return dict(repos=deepcopy(repos), capabilities=capabilities or {},
                identity=identity or dict(verification="verifier", task="task-runner",
                                          task_env=["GITHUB_TOKEN"]))


class Box:
    def __init__(self, state=None, registered=None):
        self.state, self.registered = state, registered or {}
        self.writes, self.reads = [], []

    def projects(self):
        return self.registered

    def status(self, name):
        self.reads.append(("status", name))
        return deepcopy(self.state)

    def confirm(self, plan):
        self.writes.append(("confirm", plan))

    def resume(self, name):
        self.writes.append(("resume", name))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"onboarding status must not call box.{name}")


def cli(box, *argv, stdin=""):
    import project_cli
    out, err = io.StringIO(), io.StringIO()
    code = project_cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, box=box)
    return code, out.getvalue(), err.getvalue()


def status_of(box, name="analytics"):
    return cli(box, "project", "status", name)


def lines_about(text, repo):
    return "\n".join(l for l in text.splitlines() if repo in l)


def test_an_unapproved_organization_installation_is_explained_and_resumed_with_the_same_project():  # S14
    action = "Ask the example organization administrator to approve the agent-ops App installation"
    waiting = status({REPO: repo_state(dict(state="awaiting_admin", action=action),
                                       dict(state="pending", denied=[], receipts=[], cleanup=[]))},
                     capabilities=BOARD_OK)
    box = Box(waiting, registered={"analytics": [REPO]})
    code, out, err = status_of(box)
    assert code == 0 and not err
    assert action in out and "Onboarding: incomplete" in out
    assert "Onboarding: complete" not in out
    box.state = status({REPO: repo_state()}, capabilities=BOARD_OK)  # the administrator approved
    code, out, err = cli(box, "project", "add", "analytics", REPO, stdin="yes\n")
    assert code == 0
    assert box.writes == [("resume", "analytics")]
    code, out, err = status_of(box)
    assert "Onboarding: complete" in out


def test_successful_writes_are_recorded_and_temporary_resources_cleaned_and_a_failed_cleanup_reported():  # S20
    ok = repo_state()
    box = Box(status({REPO: ok}, capabilities=BOARD_OK), registered={"analytics": [REPO]})
    code, out, err = status_of(box)
    assert code == 0 and not err
    assert "passed" in lines_about(section(out, "Write verification:"), REPO)
    cleanup = section(out, "Cleanup:")
    assert "agent-ops/verify-1" in cleanup and "removed" in cleanup
    assert "pr-9" in cleanup and "closed" in cleanup
    assert "Onboarding: complete" in out
    stuck = repo_state()
    stuck["verification"]["cleanup"][1] = dict(id="pr-9", state="failed", detail="PR is locked")
    code, out, err = status_of(Box(status({REPO: stuck})))
    cleanup = section(out, "Cleanup:")
    assert "pr-9" in cleanup and "failed" in cleanup and "PR is locked" in cleanup
    assert "agent-ops/verify-1" in cleanup and "removed" in cleanup


def test_branch_and_pr_receipts_without_a_board_write_never_report_onboarding_complete():  # S20
    box = Box(status({REPO: repo_state()}), registered={"analytics": [REPO]})
    code, out, err = status_of(box)
    assert code == 0 and not err
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out
    refused = {BOARD_WRITE: dict(documented=True, effective=False, input="present")}
    code, out, err = status_of(Box(status({REPO: repo_state()}, capabilities=refused)))
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


def test_clone_access_alone_does_not_report_onboarding_when_the_branch_push_is_denied():  # S21
    denied = repo_state(verification=failed(["push verification branch"]))
    code, out, err = status_of(Box(status({REPO: denied}, capabilities=BOARD_OK),
                                   registered={"analytics": [REPO]}))
    assert code == 0
    line = lines_about(section(out, "Write verification:"), REPO)
    assert "failed" in line and "push verification branch" in line
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


def test_a_backend_pr_denial_is_named_while_frontend_progress_is_retained_and_onboarding_incomplete():  # S60
    shop = {FRONT: repo_state(),
            BACK: repo_state(verification=failed(["create pull request"],
                                                 [dict(kind="branch", id="agent-ops/verify-2")]))}
    box = Box(status(shop, capabilities=BOARD_OK), registered={"shop": [FRONT, BACK]})
    code, out, err = status_of(box, "shop")
    verification = section(out, "Write verification:")
    assert "create pull request" in lines_about(verification, BACK)
    assert "failed" in lines_about(verification, BACK)
    assert "passed" in lines_about(verification, FRONT)
    assert "agent-ops/verify-1" in section(out, "Cleanup:")  # frontend progress retained
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


def test_granted_repositories_need_no_manual_token_and_blocked_grants_give_a_resumable_step():  # R7
    blocked = dict(state="unsupported",
                   action="The App cannot be granted this repository; install it from the repository settings, then rerun `agent-ops project add shop`")
    shop = {FRONT: repo_state(), BACK: repo_state(blocked)}
    code, out, err = status_of(Box(status(shop, capabilities=BOARD_OK),
                                   registered={"shop": [FRONT, BACK]}), "shop")
    auth = section(out, "Authorization:")
    assert "granted" in lines_about(auth, FRONT)
    assert "token" not in lines_about(auth, FRONT).lower()
    assert blocked["action"] in lines_about(auth, BACK) or blocked["action"] in auth
    assert "Onboarding: incomplete" in out


def test_every_member_repository_is_verified_separately_so_a_frontend_denial_is_not_hidden_by_backend_success():  # R11
    shop = {FRONT: repo_state(verification=failed(["open pull request"])), BACK: repo_state()}
    code, out, err = status_of(Box(status(shop, capabilities=BOARD_OK),
                                   registered={"shop": [FRONT, BACK]}), "shop")
    verification = section(out, "Write verification:")
    assert "failed" in lines_about(verification, FRONT)
    assert "passed" in lines_about(verification, BACK)
    assert "Onboarding: incomplete" in out


def test_cleanup_is_reported_per_receipt_and_never_claims_unrelated_resources():  # R11
    ok = repo_state()
    ok["verification"]["cleanup"][0] = dict(id="agent-ops/verify-1", state="failed",
                                            detail="branch protected")
    code, out, err = status_of(Box(status({REPO: ok})))
    cleanup = section(out, "Cleanup:")
    assert "agent-ops/verify-1" in cleanup and "branch protected" in cleanup
    assert "main" not in cleanup.replace("remain", "")
    assert "pr-9" in cleanup and "closed" in cleanup


def test_a_verification_that_shares_the_task_identity_or_injects_credentials_is_a_violation():  # R11
    for bad in (dict(verification="task-runner", task="task-runner", task_env=["GITHUB_TOKEN"]),
                dict(verification="verifier", task="task-runner",
                     task_env=["GITHUB_TOKEN", "APP_PRIVATE_KEY"])):
        code, out, err = status_of(Box(status({REPO: repo_state()}, identity=bad)))
        assert code != 0
        assert "identity" in err.lower() or "APP_PRIVATE_KEY" in err
        assert "Onboarding: complete" not in out


def test_documented_api_support_is_not_effective_authorization_and_missing_inputs_stay_blocked():  # R7
    caps = {**BOARD_OK,
            "create installation token": dict(documented=True, effective=None, input="missing"),
            "open pull request": dict(documented=True, effective=True, input="present")}
    code, out, err = status_of(Box(status({REPO: repo_state()}, capabilities=caps),
                                   registered={"analytics": [REPO]}))
    lines = section(out, "Capabilities:")
    token = lines_about(lines, "create installation token")
    assert "documented" in token and "blocked" in token and "effective" not in token.replace(
        "not effective", "")
    assert "effective" in lines_about(lines, "open pull request")
    assert "Onboarding: incomplete" in out


def section(text, heading):
    lines = text.splitlines()
    assert any(l.strip() == heading for l in lines), f"no {heading!r} section in:\n{text}"
    body = []
    for line in lines[next(i for i, l in enumerate(lines) if l.strip() == heading) + 1:]:
        if not line.strip():
            break
        body.append(line)
    return "\n".join(body)


def test_a_capability_with_present_input_that_is_not_effective_is_reported_not_effective():  # R7
    caps = {**BOARD_OK, "open pull request": dict(documented=True, effective=False, input="present")}
    code, out, err = status_of(Box(status({REPO: repo_state()}, capabilities=caps),
                                   registered={"analytics": [REPO]}))
    line = lines_about(section(out, "Capabilities:"), "open pull request")
    assert "not effective" in line and "blocked" not in line
    assert "Onboarding: incomplete" in out


def test_a_member_repository_without_verification_evidence_keeps_onboarding_incomplete():  # S60
    box = Box(status({FRONT: repo_state()}, capabilities=BOARD_OK),
              registered={"shop": [FRONT, BACK]})
    code, out, err = status_of(box, "shop")
    assert code == 0 and not err
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


def test_a_project_with_no_verification_evidence_at_all_is_never_reported_complete():  # S60
    code, out, err = status_of(Box(status({}, capabilities=BOARD_OK),
                                   registered={"analytics": [REPO]}))
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


class Forge:
    """Stateful GitHub stand-in: what the verifier really created is what stays live."""

    def __init__(self, admin_approved=True, denied=None):
        self.admin_approved, self.denied = admin_approved, denied or {}
        self.live, self.attempts, self.counter = {}, [], 0

    def authorize(self, repo):
        if not self.admin_approved:
            return dict(state="awaiting_admin", action="Ask the administrator to approve the App")
        return granted()

    def write(self, repo, kind):
        self.attempts.append((repo, kind))
        if self.denied.get(repo) == kind:
            raise PermissionError(kind)
        self.counter += 1
        self.live[f"{kind}-{self.counter}"] = (repo, kind)
        return f"{kind}-{self.counter}"

    def cleanup(self, repo, receipt):
        if self.live.pop(receipt["id"], None) is None:
            return dict(state="failed", detail="not found")
        return dict(state="closed" if receipt["kind"] == "pr" else "removed", detail="")


class ControlBox(Box):
    """The box side: `status` is read from what ProjectControl.verify_writes persisted."""

    def __init__(self, tmp_path, forge, repos):
        from project_control import ProjectControl

        class Github:
            def main(self):
                return "s1", {"projects": {}}

        self.pc = ProjectControl(tmp_path, Github(), None, verifier=forge)
        self.op = self.pc.confirm(dict(name="analytics", revision="r", models="inherit", board=None,
                                       repositories=[dict(repo=r) for r in repos]))
        super().__init__(registered={"analytics": list(repos)})
        self.records = {}

    def verify(self):
        self.records = self.pc.verify_writes(self.op)

    def status(self, name):
        repos = {}
        for repo, rec in self.records.items():
            repos[repo] = dict(authorization=rec["authorization"],
                               verification=dict(state=rec.get("state", "pending"),
                                                 denied=rec.get("denied", []),
                                                 receipts=rec["receipts"], cleanup=rec.get("cleanup", [])))
        return status(repos, capabilities=BOARD_OK)


def test_onboarding_completes_only_from_writes_the_verifier_really_made_and_really_cleaned(tmp_path):  # S20
    forge = Forge()
    box = ControlBox(tmp_path, forge, [REPO])
    box.verify()
    assert forge.attempts == [(REPO, "branch"), (REPO, "pr")]  # the writes were executed
    assert forge.live == {}  # and every temporary resource was removed
    code, out, err = status_of(box)
    assert "passed" in lines_about(section(out, "Write verification:"), REPO)
    assert "branch-1" in section(out, "Cleanup:") and "pr-2" in section(out, "Cleanup:")
    assert "Onboarding: complete" in out


def test_an_unapproved_installation_blocks_every_write_until_it_is_approved(tmp_path):  # S14
    forge = Forge(admin_approved=False)
    box = ControlBox(tmp_path, forge, [REPO])
    box.verify()
    assert forge.attempts == [] and forge.live == {}
    code, out, err = status_of(box)
    assert "Ask the administrator" in out and "Onboarding: incomplete" in out
    forge.admin_approved = True
    box.verify()
    assert forge.attempts == [(REPO, "branch"), (REPO, "pr")] and forge.live == {}
    assert "Onboarding: complete" in status_of(box)[1]


def test_a_denied_pull_request_is_named_and_the_retry_after_the_grant_finishes_cleanly(tmp_path):  # S21 S60
    forge = Forge(denied={BACK: "pr"})
    box = ControlBox(tmp_path, forge, [FRONT, BACK])
    box.verify()
    code, out, err = status_of(box)
    line = lines_about(section(out, "Write verification:"), BACK)
    assert "failed" in line and "pr" in line and "passed" in lines_about(section(out, "Write verification:"), FRONT)
    assert "Onboarding: incomplete" in out and forge.live == {}
    forge.denied = {}
    box.verify()
    assert forge.attempts.count((FRONT, "branch")) == 1  # the passed repository is not rewritten
    assert forge.live == {} and "Onboarding: complete" in status_of(box)[1]


def test_onboarding_is_not_deployed_while_a_member_repository_write_is_denied(tmp_path):  # S21 S60
    from project_control import ProjectControl
    from tests.test_project_control_board_steps import Boards

    class Github:
        sha, merged = "s1", False

        def main(self):
            return self.sha, {"projects": {"analytics": {"repositories": [REPO]}} if self.merged else {}}

        def find_pr(self, branch):
            return None

        def create_pr(self, branch, base_sha, config, title):
            return dict(number=1, url="u", head_sha="h", base_sha=base_sha, config=config)

        def checks(self, number):
            return dict(state="passing", failing=[])

        def approval(self, number):
            return dict(required=False, granted=True)

        def merge(self, number, head_sha):
            self.merged, self.sha = True, "m1"
            return self.sha

    class Deployer:
        def applied(self):
            return dict(revision="m1")

        def verify(self, revision):
            return dict(ok=True, detail="")

    forge = Forge(denied={REPO: "pr"})
    pc = ProjectControl(tmp_path, Github(), Deployer(), Boards(), verifier=forge)
    op = pc.confirm(dict(name="analytics", revision="r", models="inherit", board=dict(owner="example", number=7),
                         repositories=[dict(repo=REPO)], access=[], secret_refs=[], write_tests=[],
                         board_prep=dict(fields=[], options=[], labels={}),
                         backlog=dict(add={}, future="reconcile", status=None, labels=[])))
    for _ in range(4):
        pc.advance(op)
    state = pc.status(op)
    assert state["phase"] != "complete" and state["applied_revision"] is None
    assert REPO in state["blocker"]
    forge.denied = {}
    for _ in range(4):
        pc.advance(op)
    assert pc.status(op)["phase"] == "complete" and forge.live == {}
