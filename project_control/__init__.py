"""CONTROL seam: durable, confirmed project operations.

A confirmed operation lives in a file under the state directory, so a new
ProjectControl on the same directory is the same service after a restart or a
dropped SSH session. `advance` is one worker pass: it records what it
observed and returns, never waiting for checks, approval or deployment, and
never taking the shared convergence lock. The control credential stays inside
the `github` object; nothing here stores it.

See tests/test_project_control_pr_deployment_acceptance.py for the base contract.
Recovery additionally reads `merged` and `merge_sha` from find_pr; unknown
merge results remain blocked. Stale PRs use update_pr(number, expected_head,
base_sha, config), which must compare the head atomically and invalidate prior
checks and approval. No capability to refresh means an explicit blocker.

Board work (creating the board, adding missing structure and labels, the
initial import) runs before publication through the optional `boards` object,
every method idempotent and repository-qualified:
  board_state(ref) -> {owner, number, id, fields, cards}
                                 trusted, complete current identity/schema/membership read
  find(owner, title) -> [ref]      exact title lookup, including the creation identity
  create(owner, title) -> ref      ref = {"owner", "number", "id"}
  apply(ref, board_prep) -> dict   add only what is missing; returns {"fields": {name: id}}
  import_issues(ref, add)          add {repo: [number]} issues lacking a card, no status, no label
The verified ref and field mappings are recorded in the operation and become
the project's board in the published delta.
Creation uses a persisted random suffix in the title to distinguish its board
from unrelated boards with the requested title. Missing or ambiguous recovery
results block until resolved; they never authorize another creation.
"""
import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from dispatcher.config import normalize_repo
from project_control.board_validation import _check_board
from project_control.write_verification import verification_complete, verify_repo, verify_target


class UnconfirmedOperation(Exception):
    """advance/status named an operation that was never confirmed."""


class ConflictingConfirmation(Exception):
    """A confirmation cannot safely replace the recorded operation."""


class ProjectControl:
    def __init__(self, state_dir, github, deployer, boards=None, verifier=None):
        self.state_dir, self.github, self.deployer, self.boards = state_dir, github, deployer, boards
        self.verifier = verifier

    def propose(self, plan):
        return {"revision": plan["revision"], "delta": _delta(plan)}

    def confirm(self, plan, *, supersede=False):
        """Explicit superseding confirmation refreshes the expected project state.

        Ordinary retries are idempotent. Superseding is forbidden once a merge
        may have happened; that outcome must first be reconciled.
        """
        plan = _canonical_plan(plan)
        project_id = _project_id(plan["name"])
        with self._locked(project_id):
            op_id = self._operation_for_project(plan["name"], project_id)
            if not self._path(op_id).exists():
                expected = self.github.main()[1]["projects"].get(plan["name"])
                self._save(op_id, dict(plan=plan, expected=expected, phase="published", blocker="", pr=None,
                                       head_sha=None, base_sha=None, merge_sha=None,
                                       merging=False, applied_revision=None))
            else:
                self._reconfirm(op_id, plan, supersede)
        return op_id

    def _operation_for_project(self, name, project_id):
        """Keep pre-existing operation IDs and their uncertain-write history."""
        matches = [path.stem for path in self._path(project_id).parent.glob("*.json")
                   if json.loads(path.read_text())["plan"]["name"] == name]
        if len(matches) > 1:
            raise ConflictingConfirmation("multiple recorded operations require reconciliation for " + name)
        return matches[0] if matches else project_id

    def _reconfirm(self, op_id, plan, supersede):
        op = self._load(op_id)
        if not supersede:
            if _canonical_plan(op["plan"]) != plan:
                raise ConflictingConfirmation(op_id)
            return
        expected = self.github.main()[1]["projects"].get(plan["name"])
        if op["plan"] == plan and op["expected"] == expected:
            return
        if op["merging"] or op["merge_sha"] is not None:
            raise ConflictingConfirmation("resolve the previous merge before superseding " + op_id)
        op.setdefault("confirmations", []).append(dict(plan=op["plan"], expected=op["expected"]))
        op.update(plan=plan, expected=expected, phase="published", blocker="")
        op.pop("board_done", None)
        intent = plan["board"] and plan["board"].get("create") and dict(owner=plan["board"]["owner"],
                                                                         title=plan["board"]["title"])
        if not intent or intent != op.get("board_intent"):
            op.pop("board", None)  # a saved board belongs to the creation intent that made it
        self._save(op_id, op)

    def status(self, operation_id):
        op = self._load(operation_id)
        return {k: op[k] for k in ("phase", "blocker", "pr", "applied_revision")}

    def advance(self, operation_id):
        name = self._load(operation_id)["plan"]["name"]
        with self._locked(_project_id(name)):
            op = self._load(operation_id)
            if op["phase"] == "complete":
                return  # verified completion is history; later deployments do not undo it
            if op["merge_sha"] is None and self._prepare_board(operation_id, op):
                self._publish(operation_id, op)
            if op["merge_sha"] is not None and self._writes_verified(operation_id, op):
                self._deploy(op)
            self._save(operation_id, op)

    def verify_writes(self, operation_id):
        """Authorize and write-test every member repository; return the per-repository records."""
        name = self._load(operation_id)["plan"]["name"]
        with self._locked(_project_id(name)):
            op = self._load(operation_id)
            records = self._verify(operation_id, op)
            self._save(operation_id, op)
            return records

    def _verify(self, operation_id, op):
        records = op.setdefault("verification", {})
        if self.verifier is None:
            self._block(op, "write verification unavailable: no verifier configured")
            return records
        for r in op["plan"]["repositories"]:
            repo = r["repo"]
            verify_repo(self.verifier, repo, records.setdefault(repo, {}),
                        lambda: self._save(operation_id, op))
        return records

    def _writes_verified(self, operation_id, op):
        """Deployment requires repository and board writes, including their cleanup."""
        if self.verifier is None:
            self._block(op, "write verification unavailable: no verifier configured")
            return False
        bad = [repo for repo, rec in self._verify(operation_id, op).items()
               if not verification_complete(rec)]
        board = op.get("board") or op["plan"]["board"]
        if not board or not board.get("id"):
            bad.append("board identity unavailable")
        else:
            target = "board:" + board["id"]
            record = op.setdefault("board_verification", {})
            verify_target(self.verifier, target, record,
                          lambda: self._save(operation_id, op), ("board",))
            if not verification_complete(record):
                bad.append(target)
        if bad:
            self._block(op, f"write verification incomplete for {', '.join(bad)}")
        return not bad

    def _prepare_board(self, op_id, op):
        """Run confirmed work once and revalidate before each publication attempt."""
        plan = op["plan"]
        board, prep, backlog = plan["board"], plan.get("board_prep"), plan.get("backlog")
        add = {r: ns for r, ns in (backlog or {}).get("add", {}).items() if ns}
        prep = prep and any([prep["fields"], prep["options"], *prep["labels"].values()])
        if board is None:
            self._block(op, "board selection required")
            return False
        if self.boards is None:
            self._block(op, "board operations unavailable: no board adapter configured")
            return False
        try:
            if board.get("create") and "board" not in op:
                ref = self._create_board(op_id, op, board)
                if ref is None:
                    return False
                op["board"] = dict(ref)
                self._save(op_id, op)
            ref = op.get("board") or board
            self._validate_board(ref, plan, prepared=False)
            if not op.get("board_done"):
                self._apply_board(op_id, op, ref, prep, add)
            op["board"] = self._validate_board(op.get("board") or ref, plan, prepared=True)
        except Exception as e:  # noqa: BLE001 — an adapter failure is a resumable blocker
            self._block(op, f"board preparation failed: {e}")
            return False
        op["board_done"] = True
        return True

    def _apply_board(self, op_id, op, ref, prep, add):
        plan = op["plan"]
        if prep or plan["board"].get("create"):
            mappings = self.boards.apply(ref, plan.get("board_prep") or dict(fields=[], options=[], labels={}))
            op["board"] = {**ref, **mappings}
            self._save(op_id, op)
        if add:
            self.boards.import_issues(op.get("board") or ref, add)

    def _validate_board(self, ref, plan, *, prepared):
        """Always inspect through the trusted adapter, including retries with no additions."""
        state = self.boards.board_state(ref)
        for key in ("owner", "number", "id"):
            if not state.get(key) or (key in ref and state[key] != ref[key]):
                raise ValueError(f"board identity mismatch or unavailable: {key}")
        members = [r["repo"] for r in plan["repositories"]]
        problems, fields, options = _check_board(state, members)
        if problems:
            raise ValueError("; ".join(problems))
        prep = plan.get("board_prep") or dict(fields=[], options=[])
        if prepared:
            if fields or options:
                raise ValueError("required board structure is still missing")
        elif any(f not in prep["fields"] for f in fields) or any(o not in prep["options"] for o in options):
            raise ValueError("missing board structure was not confirmed")
        if prepared:
            return {**ref, "id": state["id"],
                    "fields": {f["name"]: f["id"] for f in state["fields"]}}

    def _create_board(self, op_id, op, board):
        intent = dict(owner=board["owner"], title=board["title"])
        if "board_intent" in op:
            # A legacy title-only intent cannot prove ownership of a found board.
            title = op.get("creation_title")
            found = self.boards.find(board["owner"], title) if title else []
            if op["board_intent"] == intent and len(found) == 1:
                return found[0]
            self._block(op, "board creation outcome unresolved; select a verified board "
                            "explicitly and confirm again")
            return None
        if self.boards.find(board["owner"], board["title"]):
            self._block(op, f"a board titled {board['title']!r} already exists; "
                            "select it explicitly and confirm again")
            return None
        title = f"{board['title']} [onboarding {uuid.uuid4().hex}]"
        op.update(board_intent=intent, creation_title=title)
        self._save(op_id, op)  # persist the identity before the potentially uncertain write
        return self.boards.create(board["owner"], title)

    def _publish(self, op_id, op):
        gh, plan = self.github, _effective(op)
        name, delta = plan["name"], _delta(plan)
        main_sha, main_config = gh.main()
        branch = f"onboard/{name}"
        pr = gh.find_pr(branch)
        current = main_config["projects"].get(name)
        if op["merging"] and self._recover_merge(op_id, op, pr):
            return
        if current not in (op["expected"], delta):
            return self._block(op, "project changed since it was confirmed; confirm again with supersede=True")
        taken = _owner(main_config, plan, exclude=name)
        config = {**main_config, "projects": {**main_config["projects"], name: delta}}
        if taken:
            return self._block(op, f"repository already owned by project {taken}")
        if pr is None:
            if op["pr"] is not None:
                return self._block(op, "recorded PR unavailable; resolve its outcome before publication")
            # Persist the intended publication before a possibly lost response.
            op.update(pr_config=config, base_sha=main_sha)
            self._save(op_id, op)
            pr = gh.create_pr(branch, main_sha, config, f"Onboard {name}")
        if op["pr"] is not None and pr["number"] != op["pr"]["number"]:
            return self._block(op, "recorded PR identity changed")
        if op["pr"] is None and pr["base_sha"] == op["base_sha"] and pr["config"] == op.get("pr_config"):
            op["published_head"] = pr["head_sha"]
        op["pr"] = {"number": pr["number"], "url": pr["url"]}
        if self._accept_refresh(op, pr):
            self._save(op_id, op)
        if pr["config"] != op.get("pr_config", config):
            return self._block(op, "PR content differs from the confirmed delta")
        if pr["base_sha"] != main_sha or pr["config"] != config:
            return self._refresh_pr(op_id, op, pr, main_sha, config)
        op.update(pr_config=config, base_sha=pr["base_sha"], published_head=pr["head_sha"])
        self._save(op_id, op)
        self._merge_if_ready(op_id, op, pr)

    def _merge_if_ready(self, op_id, op, pr):
        gh = self.github
        checks = gh.checks(pr["number"])
        if checks["state"] == "failing":
            return self._block(op, f"required checks failing: {', '.join(checks['failing'])}")
        if checks["state"] == "pending":
            return self._wait(op)
        approval = gh.approval(pr["number"])
        if approval["required"] and not approval["granted"]:
            return self._block(op, "organization approval required and not granted")
        op["head_sha"], op["merging"] = pr["head_sha"], True
        self._save(op_id, op)
        op["merge_sha"] = gh.merge(pr["number"], op["head_sha"])
        op["phase"], op["blocker"] = "merged", ""
        self._save(op_id, op)

    def _recover_merge(self, op_id, op, pr):
        """Only GitHub's recorded PR merge result can resolve an uncertain write."""
        if pr is None or op["pr"] is None or pr["number"] != op["pr"]["number"]:
            self._block(op, "merge outcome unresolved: recorded PR unavailable")
        elif pr["head_sha"] != op["head_sha"]:
            self._block(op, "merge outcome unresolved: PR head changed")
        elif pr.get("merged") is False:
            op["merging"] = False
            return False  # GitHub confirms no merge; run every publication check again.
        elif not pr.get("merged"):
            self._block(op, "merge outcome unresolved: GitHub must report the PR merge result")
        elif not pr.get("merge_sha"):
            self._block(op, "merge outcome unresolved: actual PR merge revision unavailable")
        else:
            op.update(merge_sha=pr["merge_sha"], blocker="", phase="merged")
        self._save(op_id, op)
        return True

    @staticmethod
    def _accept_refresh(op, pr):
        refresh = op.get("refresh")
        if refresh and pr["base_sha"] == refresh["base_sha"] and pr["config"] == refresh["config"]:
            op.update(pr_config=pr["config"], base_sha=pr["base_sha"], published_head=pr["head_sha"])
            op.pop("refresh")
            return True
        return False

    def _refresh_pr(self, op_id, op, pr, main_sha, config):
        """Rebuild only an unchanged, previously authorized PR, retaining its identity.

        GitHub adapters implement update_pr(number, expected_head, base_sha,
        config), atomically compare the head and reset checks/approval for the
        returned new head. A lost response is reconciled from the saved intent.
        """
        if pr["head_sha"] != op.get("published_head"):
            return self._block(op, "PR head changed before refresh; review required")
        update = getattr(self.github, "update_pr", None)
        if update is None:
            return self._block(op, "PR refresh unavailable: GitHub adapter must support update_pr")
        op["refresh"] = dict(base_sha=main_sha, config=config)
        self._save(op_id, op)
        refreshed = update(pr["number"], pr["head_sha"], main_sha, config)
        if refreshed["number"] != pr["number"] or not self._accept_refresh(op, refreshed):
            return self._block(op, "PR refresh did not publish the confirmed delta")
        self._wait(op)  # Observe checks and approval for the new head on the next pass.
        self._save(op_id, op)

    def _deploy(self, op):
        revision = self.deployer.applied()["revision"]
        if revision != op["merge_sha"] and not self._carries_delta(op, revision):
            op["phase"], op["blocker"] = "merged", ""
            return
        verdict = self.deployer.verify(revision)
        if not verdict["ok"]:
            return self._block(op, f"deployment incompatible: {verdict['detail']}")
        op.update(phase="complete", blocker="", applied_revision=revision)

    def _carries_delta(self, op, revision):
        """A later deployment that still holds the confirmed delta covers the merge."""
        sha, config = self.github.main()
        return revision == sha and config["projects"].get(op["plan"]["name"]) == _delta(_effective(op))

    @staticmethod
    def _wait(op):
        op["phase"], op["blocker"] = "published", ""

    @staticmethod
    def _block(op, why):
        pr = op["pr"]
        op["phase"], op["blocker"] = "blocked", why + (f" (PR #{pr['number']} {pr['url']})" if pr else "")

    def _path(self, op_id):
        return Path(self.state_dir) / "operations" / f"{op_id}.json"

    @contextmanager
    def _locked(self, op_id):
        path = self._path(op_id).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _load(self, op_id):
        try:
            return json.loads(self._path(op_id).read_text())
        except FileNotFoundError:
            raise UnconfirmedOperation(op_id) from None

    def _save(self, op_id, op):
        path = self._path(op_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(op))
        os.replace(tmp, path)


def _effective(op):
    """The confirmed plan with the board the operation verified, if it had to make one."""
    return {**op["plan"], "board": op["board"]} if op.get("board") else op["plan"]


def _delta(plan):
    """New projects start paused; enabling is a separately confirmed operation."""
    return {"repositories": _canonical_plan(plan)["repositories"], "board": plan["board"],
            "desired_enabled": False, "models": plan["models"]}


def _owner(config, plan, exclude):
    mine = {normalize_repo(r["repo"]) for r in plan["repositories"]}
    for name, project in config["projects"].items():
        theirs = {normalize_repo(r if isinstance(r, str) else r["repo"])
                  for r in project.get("repositories", [])}
        if name != exclude and mine & theirs:
            return name


def _canonical_plan(plan):
    return {**plan, "repositories": sorted(
        ({**r, "repo": normalize_repo(r["repo"])} for r in plan["repositories"]),
        key=lambda r: r["repo"])}


def _project_id(name):
    """Repository membership can change without changing operation ownership."""
    return "op-" + hashlib.sha256(name.encode()).hexdigest()[:16]
