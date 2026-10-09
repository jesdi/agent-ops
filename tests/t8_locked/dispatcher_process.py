"""OWN isolated driver of public run_pass and its declared external objects.

No production functions are replaced. Usage/inbound/triage/artifact inputs use their
public owned artifacts/environment. Session observations always read RuntimeClient;
the recording session object is a HOST external-boundary stand-in, never native proof.
"""
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from public_errors import install

install()


def main(path):
    spec = json.loads(Path(path).read_text())
    state_dir = Path(spec["state_dir"])
    config = importlib.import_module("dispatcher.config")
    models = importlib.import_module("dispatcher.models")
    runtime = importlib.import_module("dispatcher.runtime_http")
    dispatcher = importlib.import_module("dispatcher.main")
    client = runtime.RuntimeClient(state_dir)
    trace, calls, sent, ended, spawned, resumed, github_calls = [], [], [], [], [], [], []
    dead = set(tuple(key) for key in spec.get("dead", []))

    class GitHub:
        def record(self, method, *args):
            github_calls.append({"method": method, "args": [getattr(arg, "name", arg) for arg in args]})
        def viewer_login(self):
            self.record("viewer_login")
            return "fixture-owner"
        def candidates(self, target):
            self.record("candidates", target)
            return [SimpleNamespace(**item) for item in spec.get("candidates", {}).get(target.name, [])]
        def rank_rows(self, target):
            self.record("rank_rows", target)
            return []
        def issue_state(self, repo, number):
            self.record("issue_state", repo, number)
            return "OPEN"
        def issue_view(self, repo, number):
            self.record("issue_view", repo, number)
            return {"number": number, "title": "generic OWN issue", "body": "generic OWN issue body",
                    "state": "OPEN", "labels": [], "url": f"https://github.com/{repo}/issues/{number}"}
        def pr_view(self, target, pr_number):
            self.record("pr_view", target, pr_number)
            return {"number": pr_number, "state": spec.get("pr_state", "OPEN"),
                    "headRefName": spec.get("pr_branch", "fixture-branch"), "headRefOid": "1" * 40,
                    "baseRefName": "main", "mergeable": "MERGEABLE", "reviewDecision": "REVIEW_REQUIRED",
                    "statusCheckRollup": [], "reviews": [], "comments": [],
                    "mergedAt": datetime.now(timezone.utc).isoformat() if spec.get("pr_state") == "MERGED" else None}
        def ci_statuses(self, target, head, branch):
            self.record("ci_statuses", target, head, branch)
            return [SimpleNamespace(conclusion="success", completed_at=datetime.now(timezone.utc).isoformat())]
        def pr_number_for_branch(self, target, branch):
            self.record("pr_number_for_branch", target, branch)
            return 55
        def run_status(self, target, run_id):
            self.record("run_status", target, run_id)
            return spec.get("run_status", "")
        def create_issue(self, repo, title, body):
            self.record("create_issue", repo, title, body)
            raise AssertionError("fixture forbids unrelated issue creation")
        def set_status(self, target, issue, option_id): self.record("set_status", target, issue, option_id)
        def set_boost(self, target, issue, value): self.record("set_boost", target, issue, value)
        def add_label(self, target, issue, label): self.record("add_label", target, issue, label)
        def comment(self, target, issue, body): self.record("comment", target, issue, body)
        def claim(self, target, cand): self.record("claim", target, cand.number)
        def release(self, target, issue, reason): self.record("release", target, issue, reason)
        def cancel(self, target, issue): self.record("cancel", target, issue)
        def delete_branch(self, target, branch): self.record("delete_branch", target, branch)
        def append_blocked_by(self, target, issue, blocker): self.record("append_blocked_by", target, issue, blocker)

    class Sessions:
        def runtime_view(self, target, issue): return client.view(target, issue)
        def is_alive(self, target, issue): return (target, issue) not in dead
        def capture_tail(self, target, issue, lines=25): return "generic OWN recent terminal output"
        def capture_history(self, target, issue, lines=2000): return "generic OWN terminal history"
        def idle_seconds(self, target, issue): return 0.0
        def agent_state(self, target, issue): return ("working", 1)
        def forget_status(self, target, issue): trace.append(["forget_status", target, issue])
        def send_text(self, target, issue, text):
            sent.append([target, issue, text])
            trace.append(["send_text", target, issue])
        def end(self, target, issue):
            view = client.view(target, issue)
            if isinstance(view, dict) and not view.get("retired") and isinstance(view.get("binding"), dict):
                result = client.retire(view["binding"], view["revision"], reason="forced")
                if result != "retired":
                    raise AssertionError("OWN external session closure must complete its public retirement fence")
            ended.append([target, issue])
            trace.append(["end", target, issue])
            dead.add((target, issue))
        def spawn_stage(self, target, issue, worktree, prompt, stage_name, model, effort="", second=None, *, ticket=""):
            spawned.append({"target": target, "issue": issue, "stage": stage_name, "ticket": ticket})
            trace.append(["spawn_stage", target, issue])
            client.prepare(target, issue, stage_name, ticket=ticket, worktree=worktree)
            dead.discard((target, issue))
        def resume(self, target, issue, worktree, message, model, effort="", second=None, *, session_id):
            resumed.append({"target": target, "issue": issue, "session_id": session_id})
            trace.append(["resume", target, issue])
            client.prepare(target, issue, spec["continued_stages"][target + ":" + str(issue)],
                           conversation_id=session_id, worktree=worktree)
            dead.discard((target, issue))

    class Notifier:
        def send(self, template, **ctx):
            trace.append(["notice", template, ctx["target"], ctx["issue"]])
            calls.append({"template": template, "context": ctx})
            return 71

    def observe_public_call(frame, event, arg):
        # Interpreter observation of this one declared callable, no locals/bodies/private helpers.
        if event == "call" and frame.f_globals.get("__name__") == "dispatcher.runtime_presentation" and frame.f_code.co_name == "present_runtime_alerts":
            trace.append(["presentation-entry"])

    targets = [config.Target(**target) for target in spec["targets"]]
    entry = models.Entry(provider="openai", model="generic-fixture")
    policy = models.ModelPolicy(triage=(entry,), untracked="fixture", tracks={"fixture": models.Track(
        name="fixture", when="generic owned host fixture", stages={stage: (entry,) for stage in
        ["spec", "plan", "implement", "review", "address-review"]})})
    cfg = config.Config(state_dir=str(state_dir), targets=targets, capacity=spec.get("capacity", 1),
                        session_memory="2g", session_cpus="2", background_wait_seconds=spec.get("cap", 10800), models=policy)
    sys.setprofile(observe_public_call)
    try:
        dispatcher.run_pass(cfg, dispatcher.Deps(GitHub(), Sessions(), Notifier()))
    finally:
        sys.setprofile(None)
    Path(spec["result_path"]).write_text(json.dumps({"trace": trace, "calls": calls, "sent": sent,
        "ended": ended, "spawned": spawned, "resumed": resumed, "github": github_calls}))


if __name__ == "__main__":
    main(sys.argv[1])
