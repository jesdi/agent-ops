"""The exam job's GitHub access and generation session. Only here is a token with
`statuses: write` minted: the `exam` status is posted as the App, and no task,
triage or host token holds it. The session itself gets no token and no clone."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from dispatcher import containers, exams, github_app

PERMISSIONS = {"statuses": "write", "pull_requests": "read", "contents": "read",
               "metadata": "read"}
SCRIPT = Path(__file__).resolve().parent.parent / ".my-skills" / "pr-exam" / "pr_exam.py"
PROMPT = ("Follow the pr-exam skill at {skill}/SKILL.md. Read the generation request at "
          "{request}; write the exam it asks for to the `out` path it names, then run "
          "`python3 {skill}/pr_exam.py validate --request {request} <out>` and fix every "
          "error it names.")


def token(access: github_app.Access, repo: str) -> str:
    """The exam token for `repo`: minted per call, never written to disk."""
    return access.grants([repo], PERMISSIONS)[repo.lower()].token


class Github:
    """The injected GitHub of `exams.work`, over `gh api` with the exam token."""

    def __init__(self, access: github_app.Access, app: github_app.Client, *, run=subprocess.run):
        self.access, self.client, self.run = access, app, run

    def _api(self, repo: str, *args: str, accept: str = "application/vnd.github+json") -> str:
        env = {**os.environ, "GH_TOKEN": token(self.access, repo)}
        return self.run(["gh", "api", "-H", f"Accept: {accept}", *args], env=env, text=True,
                        capture_output=True, timeout=120, check=True).stdout

    def pr(self, repo: str, n: int) -> dict:
        doc = json.loads(self._api(repo, f"repos/{repo}/pulls/{n}"))
        return {"head_sha": doc["head"]["sha"], "base_ref": doc["base"]["ref"],
                "title": doc["title"], "body": doc["body"], "state": doc["state"],
                "merged": bool(doc.get("merged"))}

    def diff(self, repo: str, n: int) -> str:
        return self._api(repo, f"repos/{repo}/pulls/{n}", accept="application/vnd.github.diff")

    def file(self, repo: str, ref: str, path: str) -> str | None:
        try:
            return self._api(repo, f"repos/{repo}/contents/{path}?ref={ref}",
                             accept="application/vnd.github.raw+json")
        except subprocess.CalledProcessError as exc:
            if "404" in (exc.stderr or ""):
                return None
            raise

    def post_status(self, repo, sha, context, state, description) -> None:
        self._api(repo, "-X", "POST", f"repos/{repo}/statuses/{sha}", "-f", f"context={context}",
                  "-f", f"state={state}", "-f", f"description={description}")

    def app(self) -> dict:
        info = self.client.info()
        return {"slug": info["slug"], "id": info["id"]}


class PrExam:
    """The skill's script, run as the skill documents: `validate` and `public`."""

    def __init__(self, script: Path = SCRIPT, *, run=subprocess.run):
        self.script, self.run = script, run

    def _call(self, *args) -> subprocess.CompletedProcess:
        return self.run([sys.executable, "-I", str(self.script), *map(str, args)],
                        capture_output=True, text=True, timeout=60)

    def validate(self, exam: Path, request: Path) -> list[str]:
        proc = self._call("validate", "--request", request, exam)
        return [] if proc.returncode == 0 else (proc.stderr.splitlines() or ["validate failed"])

    def public(self, exam: Path) -> dict:
        proc = self._call("public", exam)
        if proc.returncode:
            raise ValueError(proc.stderr.strip() or "public failed")
        return json.loads(proc.stdout)


def session(cfg, *, run=subprocess.run):
    """`run(request, model=, timeout=)` of `exams.work`: one headless container whose
    only mounts are the job dir, the records (read-only), the skill and the model's auth."""
    def go(request: Path, *, model: str, timeout: int) -> None:
        job = request.parent
        spec = json.loads(request.read_text())
        prompt = job / "prompt.md"
        prompt.write_text(PROMPT.format(skill="/skill", request=request))
        name = f"exam-{job.parent.parent.name}-{job.parent.name}"
        cmd = containers.exam_cmd(name, str(job), spec["records"], str(SCRIPT.parent),
                                  cfg.session_memory, cfg.session_cpus, model, str(prompt))
        try:
            run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                run(["podman", "kill", name], capture_output=True, text=True, timeout=60)
            except Exception:
                pass
            raise
    return go


def tick(cfg, deps, usages) -> None:
    """One pass's exam work, outside task capacity. No App registered: nothing runs."""
    if not deps.app.registered():
        return
    exams.work(cfg, github=Github(deps.access, deps.app), run=session(cfg), pr_exam=PrExam(),
               notify=lambda text: deps.notifier.send("status", lines=[text]),
               now=deps.clock, usages=usages)
