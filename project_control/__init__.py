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

from dispatcher import claims
from dispatcher.config import normalize_repo
from dispatcher.convergence import claim_lock
from dispatcher.state import load_all
from project_control import provisioning
from project_control.board_validation import _check_board
from project_control.write_verification import verification_complete, verify_repo, verify_target


class UnconfirmedOperation(Exception):
    """advance/status named an operation that was never confirmed."""


class ConflictingConfirmation(Exception):
    """A confirmation cannot safely replace the recorded operation."""


class UnknownProject(Exception):
    """A control request named a project the deployed configuration lacks."""


class ProjectControl:
    def __init__(self, state_dir, github, deployer, boards=None, verifier=None, host=None):
        self.state_dir, self.github, self.deployer, self.boards = state_dir, github, deployer, boards
        self.verifier, self.host = verifier, host

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

    def request_enabled(self, name, enabled):
        """The one confirmed enable / pause operation behind the web and the terminal.

        It runs the ordinary PR / checks / approval / merge / verified-deployment
        sequence and changes only `desired_enabled` of the project's entry. The
        same pending change is the same operation.
        """
        with self._locked(_project_id(name)):
            pending = next(iter(self._control_ops(name)), None)
            if pending:
                if pending[1]["control"]["enabled"] != enabled:
                    raise ConflictingConfirmation(pending[0])
                self._rebase_control(*pending)
                return pending[0]
            current = self.github.main()[1]["projects"].get(name)
            if current is None:
                raise UnknownProject(name)
            op_id = "ctl-" + uuid.uuid4().hex
            plan = _control_plan(name, current)
            self._save(op_id, dict(plan=plan, expected=current, phase="published", blocker="", pr=None,
                                   head_sha=None, base_sha=None, merge_sha=None, merging=False,
                                   applied_revision=None, control=dict(enabled=enabled)))
            return op_id

    def _rebase_control(self, op_id, op):
        """Repeating a blocked request after the project entry changed re-bases it on the
        current entry: the delta is only `desired_enabled`, so nothing is lost. Never once
        a merge may have happened; that outcome must first be reconciled."""
        name = op["plan"]["name"]
        current = self.github.main()[1]["projects"].get(name)
        if (current is None or current in (op["expected"], _proposed(op))
                or op["merging"] or op["merge_sha"] is not None):
            return
        op.update(plan=_control_plan(name, current), expected=current, phase="published", blocker="")
        self._save(op_id, op)

    def pending_change(self, name):
        """The not yet deployed enable / pause request of `name`, or None."""
        pending = next(iter(self._control_ops(name)), None)
        if pending is None:
            return None
        op_id, op = pending
        return dict(operation=op_id, requested="enabled" if op["control"]["enabled"] else "paused",
                    phase=op["phase"], blocker=op["blocker"], pr=op["pr"])

    def _ops(self, name):
        """(id, record) of each control operation of `name`."""
        records = ((path.stem, json.loads(path.read_text()))
                   for path in sorted(self._path("x").parent.glob("*.json")))
        return [(i, op) for i, op in records if "control" in op and op["plan"]["name"] == name]

    def _all_ops(self):
        return [(p.stem, json.loads(p.read_text())) for p in sorted(self._path("x").parent.glob("*.json"))]

    def _control_ops(self, name):
        """(id, record) of each undeployed control operation of `name`."""
        return [(i, op) for i, op in self._ops(name) if op["phase"] not in ("complete", "failed")]

    def _operation_for_project(self, name, project_id):
        """Keep pre-existing operation IDs and their uncertain-write history."""
        matches = [path.stem for path in self._path(project_id).parent.glob("*.json")
                   if (op := json.loads(path.read_text()))["plan"]["name"] == name
                   and "control" not in op]
        if len(matches) > 1:
            raise ConflictingConfirmation("multiple recorded operations require reconciliation for " + name)
        return matches[0] if matches else project_id

    def _reconfirm(self, op_id, plan, supersede):
        op = self._load(op_id)
        if not supersede:
            if _canonical_plan(op["plan"]) != plan:
                raise ConflictingConfirmation(op_id)
            self._requeue(op_id, op)
            return
        expected = self.github.main()[1]["projects"].get(plan["name"])
        if op["plan"] == plan and op["expected"] == expected:
            self._requeue(op_id, op)
            return
        if op["merging"] or op["merge_sha"] is not None:
            raise ConflictingConfirmation("resolve the previous merge before superseding " + op_id)
        op.setdefault("confirmations", []).append(dict(plan=op["plan"], expected=op["expected"]))
        op.update(plan=plan, expected=expected, phase="published", blocker="")
        op.pop("board_done", None)
        if "provisioning" in op:  # a new plan needs its own verification; leftovers are still stopped
            old = op["provisioning"]
            op["provisioning"] = {**provisioning.new(), "live": old["live"], "cleanup": old["cleanup"]}
        intent = plan["board"] and plan["board"].get("create") and dict(owner=plan["board"]["owner"],
                                                                         title=plan["board"]["title"])
        if not intent or intent != op.get("board_intent"):
            op.pop("board", None)  # a saved board belongs to the creation intent that made it
        self._save(op_id, op)

    def _requeue(self, op_id, op):
        if "provisioning" in op:
            provisioning.requeue(op["provisioning"])
            self._save(op_id, op)

    def status(self, operation_id):
        op = self._load(operation_id)
        status = {k: op[k] for k in ("phase", "blocker", "pr", "applied_revision")}
        if self.host is not None and "control" not in op:  # a control request never provisions
            status["provisioning"] = provisioning.view(op.get("provisioning") or provisioning.new())
        return status

    def completion(self, operation_id, web):
        """Verify an onboarding from what is applied: the deployed revision, the web's project
        view and the box's checkouts, never from the confirmed plan alone."""
        op = self._load(operation_id)
        name = op["plan"]["name"]
        main_sha, config = self.github.main()
        entry = config["projects"].get(name)
        blockers = []
        deployed = self.deployer.applied()["revision"]
        applied = op["applied_revision"]
        if deployed == main_sha:
            if entry is None:
                blockers.append(f"deployment: {name} is not in the configuration deployed at {main_sha}")
            expected = entry
        else:  # main's configuration is not what the box runs: use verified deployment history
            expected = self._verified_project(name, deployed)
            if expected is None:
                blockers.append(f"deployment: configuration at {main_sha} of {name} is not deployed (applied {deployed})")
        if applied is None:
            blockers.append(f"deployment: {name} has no verified applied revision yet")
        tab = next((t for t in web.projects() if t["name"] == name), None)
        if tab is None:
            blockers.append(f"web: project {name} is not in the applied project view")
        elif expected is not None:
            if tab["state"] != ("enabled" if expected["desired_enabled"] else "paused"):
                blockers.append(f"web: project {name} is {tab['state']}, not as configured")
            if _members(tab["repositories"]) != _members(expected["repositories"]):
                blockers.append(f"web: project {name} serves a different repository membership than configured")
            board = expected["board"]
            if tab.get("board") != dict(owner=board["owner"], number=board["number"]):
                blockers.append(f"web: project {name} serves a different board than configured")
        entry = expected
        repos = (entry or _delta(op["plan"]))["repositories"]
        if self.host is None:
            blockers.append(f"checkout: {name} cannot be verified without a host adapter")
        else:
            blockers += [f"checkout: {r['repo']} of {name} is missing at {r['clone_path']}"
                         for r in repos if not self.host.has_checkout(r["clone_path"])]
            blockers += self.host.unresolved_inputs()
        return dict(complete=not blockers, applied_revision=deployed if applied is not None else None, blockers=blockers,
                    project=dict(state=tab and tab["state"], tasks=tab and tab["tasks"], tab=tab and name),
                    board=entry and dict(owner=entry["board"]["owner"], number=entry["board"]["number"]),
                    repositories=[dict(repo=r["repo"], checkout=r["clone_path"],
                                       present=self.host is not None and self.host.has_checkout(r["clone_path"]))
                                  for r in repos])

    def _verified_project(self, name, revision):
        """Recover the versioned entry verified at this exact deployed revision.

        Enable/pause operations carry the whole project entry, so their durable
        evidence remains usable after main moves and after service restarts.
        Pending or merely merged operations are never deployment evidence.
        """
        if revision is None:
            return None
        return next((_proposed(op) for _, op in self._all_ops()
                     if op["plan"]["name"] == name and op["phase"] == "complete"
                     and op["applied_revision"] == revision), None)

    def operations(self):
        return [path.stem for path in sorted(self._path("x").parent.glob("*.json"))]

    def verification(self):
        return provisioning.Verification(self)

    def provision(self, operation_id, slot=None):
        """Run or resume host provisioning in execution slot `slot`; None means no free slot."""
        op = self._load(operation_id)
        if "control" in op:
            raise ValueError("a control operation has no provisioning: " + operation_id)
        name = op["plan"]["name"]
        with self._locked(_project_id(name)):
            op = self._load(operation_id)
            prov = op.setdefault("provisioning", provisioning.new())
            if slot is not None and prov["state"] != "passed":
                provisioning.run(self.host, op["plan"], prov, slot, lambda: self._save(operation_id, op))
            self._save(operation_id, op)
            return provisioning.view(prov)

    def advance_controls(self):
        """Advance every undeployed enable / pause request one step; the web console's worker calls it."""
        for path in sorted(self._path("x").parent.glob("*.json")):
            op = json.loads(path.read_text())
            if "control" in op and (op["phase"] not in ("complete", "failed") or "release_pending" in op):
                try:
                    self.advance(path.stem)
                except Exception as error:  # adapter failures must not starve other projects
                    self._control_failure(path.stem, error)

    def _control_failure(self, operation_id, error):
        name = self._load(operation_id)["plan"]["name"]
        with self._locked(_project_id(name)):
            # Reload persisted intent: a lost response may hide a successful write.
            op = self._load(operation_id)
            if op["phase"] not in ("complete", "failed"):
                self._block(op, f"control advancement failed: {error}")
                self._save(operation_id, op)

    def has_live(self, operation_id):
        """Temporary resources not confirmed stopped: they keep their execution slot."""
        return bool((self._load(operation_id).get("provisioning") or {}).get("live"))

    def _provisioned(self, op):
        return self.host is None or (op.get("provisioning") or {}).get("state") == "passed"

    def advance(self, operation_id):
        name = self._load(operation_id)["plan"]["name"]
        with self._locked(_project_id(name)):
            op = self._load(operation_id)
            if op["phase"] in ("complete", "failed"):
                if "release_pending" in op:  # a crash cut the hold cleanup short: retry it
                    self._release_holds(operation_id)
                return  # verified completion is history; later deployments do not undo it
            control = "control" in op  # no board, provisioning or write verification: nothing new
            if control and not self._admitted(operation_id, op):
                return
            if op["merge_sha"] is None and (control or self._prepare_board(operation_id, op)):
                if control or self._provisioned(op):
                    self._publish(operation_id, op)
                else:
                    self._block(op, "provisioning and sandbox verification incomplete")
            if op["merge_sha"] is not None and (control or self._writes_verified(operation_id, op)):
                self._deploy(op)
            if control and op["phase"] == "complete" and "release_pending" not in op:
                # The deployed configuration now carries this state: the holds of the operations
                # complete before it are redundant. Name them in the completing save so a crash
                # cannot lose the cleanup, and a retry never touches a later operation's hold.
                op["release_pending"] = [i for i, o in self._ops(name)
                                         if i != operation_id and o.get("held") and o["phase"] == "complete"]
            self._save(operation_id, op)
            if "release_pending" in op:
                self._release_holds(operation_id)

    def _release_holds(self, operation_id):
        done = self._load(operation_id)
        for old_id in done["release_pending"]:
            old = self._load(old_id)
            self._save(old_id, {**old, "held": False})
        self._save(operation_id, {k: v for k, v in done.items() if k != "release_pending"})

    def abandon(self, operation_id):
        """Fail an operation that is not complete; its reservation hold is released."""
        name = self._load(operation_id)["plan"]["name"]
        with self._locked(_project_id(name)):
            op = self._load(operation_id)
            if op["merging"] or op["merge_sha"] is not None:
                raise ConflictingConfirmation("resolve the merge before abandoning " + operation_id)
            if op["phase"] != "complete":
                self._close_open_pr(operation_id, op)
                op.update(phase="failed", blocker="", held=False)
                self._save(operation_id, op)

    def _close_open_pr(self, op_id, op):
        """An open PR could still merge and promise capacity that is gone: close it first.

        Looks the PR up by branch even when its creation response was lost (`pr` unset)."""
        gh, name = self.github, op["plan"]["name"]
        branch = f"control/{name}/{op_id}" if "control" in op else f"onboard/{name}"
        pr = gh.find_pr(branch)
        recorded = op["pr"]["number"] if op["pr"] else None
        if pr and pr.get("state", "open") == "open" and recorded in (None, pr["number"]):
            gh.close_pr(pr["number"])
            pr = gh.find_pr(branch)
        if (pr is None and recorded is not None) or (pr and (
                pr.get("merged") or pr.get("state", "open") != "closed"
                or recorded not in (None, pr["number"]))):
            raise ConflictingConfirmation("the PR is not confirmed closed; resolve it before abandoning " + op_id)

    def _admitted(self, op_id, op):
        """An enable request holds a reservation from admission until it completes or fails.

        Admission and every claim decision run under the claims lock, so a free
        place is promised to exactly one of them. Pausing needs no place."""
        if op.get("held") or not op["control"]["enabled"]:
            return True
        name = op["plan"]["name"]
        with claim_lock(self.state_dir):
            enabled = {k for k, v in self.github.main()[1]["projects"].items() if v["desired_enabled"]}
            # a merged change not yet deployed (a pause) still leaves the dispatcher with the old state
            undeployed = {o["plan"]["name"] for _, o in self._all_ops()
                          if "control" in o  # an onboarding merge deploys the project paused
                          and o["phase"] not in ("complete", "failed") and (o["merging"] or o["merge_sha"])}
            projects = enabled | undeployed | set(claims.held_reservations(self.state_dir)) | {name}
            if (len(projects) > claims.PLACES
                    or claims.places_taken(load_all(self.state_dir), projects) > claims.PLACES):
                self._block(op, "waiting for reservation capacity: "
                                "unfinished tasks and held reservations leave no place")
                self._save(op_id, op)
                return False
            op.update(held=True)
            if op["phase"] == "blocked":
                self._wait(op)
            self._save(op_id, op)
        return True

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
        name, delta = plan["name"], _proposed(op)
        main_sha, main_config = gh.main()
        branch = f"control/{name}/{op_id}" if "control" in op else f"onboard/{name}"
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
            pr = gh.create_pr(branch, main_sha, config,
                              f"{'Enable' if op['control']['enabled'] else 'Pause'} {name}"
                              if "control" in op else f"Onboard {name}")
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
        return revision == sha and config["projects"].get(op["plan"]["name"]) == _proposed(op)

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


def _control_plan(name, current):
    return dict(name=name, repositories=current["repositories"], board=current["board"],
                models=current["models"])


def _proposed(op):
    """The project entry the operation's PR proposes."""
    if "control" in op:
        return {**op["expected"], "desired_enabled": op["control"]["enabled"]}
    return _delta(_effective(op))


def _delta(plan):
    """New projects start paused; enabling is a separately confirmed operation."""
    return {"repositories": _canonical_plan(plan)["repositories"], "board": plan["board"],
            "desired_enabled": False, "models": plan["models"]}


def _members(repositories):
    return sorted(normalize_repo(r if isinstance(r, str) else r["repo"]) for r in repositories)


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
