"""The exam job's GitHub access and generation session. Only here is a token with
`statuses: write` minted: the `exam` status is posted as the App, and no task,
triage or host token holds it. The session itself gets no token and no clone."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import sys
from pathlib import Path

from dispatcher import containers, exams, github_app, herdr, triage
from dispatcher.state import state_lock
from dispatcher.usage_providers import fetch_all

TAB = "exam-runner"
RUN_LOCK = "exam-runner.lock"
PERMISSIONS = {"statuses": "write", "pull_requests": "read", "contents": "read",
               "metadata": "read"}
# On top of triage's: the wrapper its podman sessions start through. A herdr pane inherits
# the herdr server's env, not the pass's. Never a secret: herdr gets `env` in its argv, so
# the runner decrypts the App itself (main's --exam-run, github_app.cached).
LAUNCH_ENV_VARS = ("AGENT_OPS_COMMAND_WRAPPER",)
SCRIPT = Path(__file__).resolve().parent.parent / ".my-skills" / "pr-exam" / "pr_exam.py"
PROMPT = ("Follow the pr-exam skill at {skill}/SKILL.md. Read the generation request at "
          "{request}; write the exam it asks for to the `out` path it names, then run "
          "`python3 {skill}/pr_exam.py validate --request {request} <out>` and fix every "
          "error it names.")
GRADE_PROMPT = ("Follow the pr-exam skill at {skill}/SKILL.md. Read the grading request at "
                "{request}; grade each answer against its rubric and write the grades it asks "
                "for to the `out` path it names.")


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
                "merged": bool(doc.get("merged")), "labels": [x["name"] for x in doc["labels"]]}

    def label_events(self, repo: str, n: int) -> list[dict]:
        """The PR's label events, oldest first, over every page."""
        pages = json.loads(self._api(repo, "--paginate", "--slurp",
                                     f"repos/{repo}/issues/{n}/events?per_page=100"))
        return [{"event": e["event"], "label": e["label"]["name"], "actor": (e["actor"] or {}).get("login", "")}
                for page in pages for e in page if e["event"] in ("labeled", "unlabeled")]

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

    def post_status(self, repo, sha, context, state, description, *, target_url="") -> None:
        fields = ["-f", f"context={context}", "-f", f"state={state}", "-f", f"description={description}"]
        if target_url:
            fields.extend(["-f", f"target_url={target_url}"])
        self._api(repo, "-X", "POST", f"repos/{repo}/statuses/{sha}", *fields)

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

    def config(self, path: Path) -> dict:
        proc = self._call("config", path)
        if proc.returncode:
            raise ValueError(proc.stderr.strip() or "config failed")
        return json.loads(proc.stdout)

    def score(self, exam: Path, answers: Path, grades: Path, flags: Path | None = None) -> dict:
        extra = ("--flags", flags) if flags else ()
        proc = self._call("score", "--exam", exam, "--answers", answers, "--grades", grades, *extra)
        if proc.returncode:
            raise ValueError(proc.stderr.strip() or "score failed")
        return json.loads(proc.stdout)

    def context(self, attempt_dir: Path, turn: dict) -> str:
        """The prompt of a chat turn, as the skill's `context` prints it. `turn` is the host's:
        kind, question, message and the question's finished `thread` ({kind, message, text});
        it is put in the skill's shape here. ValueError when the skill refuses the prompt (too
        long, or no such attempt)."""
        adir = Path(attempt_dir)
        if not (adir / "exam.json").exists():
            raise ValueError("no such attempt")
        return self._context(adir, _tip_doc(turn) if turn["kind"] == "tip" else _chat_doc(adir, turn))

    def _context(self, adir: Path, doc: dict) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "turn.json").write_text(json.dumps(doc))
            for name in ("diff.patch", "body.md"):   # an attempt opened before chat has neither
                (Path(tmp) / name).write_text(_text_or_empty(adir.parent.parent / name))   # the exam dir keeps the head's
            proc = self._call("context", "--exam", adir / "exam.json", "--diff", Path(tmp) / "diff.patch",
                              "--body", Path(tmp) / "body.md", "--turn", Path(tmp) / "turn.json")
        if proc.returncode:
            raise ValueError(proc.stderr.strip() or "context failed")
        return proc.stdout

    def record(self, records: Path, exam: Path, result: Path, today: str) -> None:
        proc = self._call("record", "--records", records, "--exam", exam, "--result", result,
                          "--today", today)
        if proc.returncode:
            raise ValueError(proc.stderr.strip() or "record failed")


def _tip_doc(turn: dict) -> dict:
    tips = [t["text"] for t in turn.get("thread", []) if t["kind"] == "tip"]
    return {"kind": "tip", "question": turn.get("question"), "draft": turn["message"],
            "tip": 1 + len(tips), "tips": tips}


def _chat_doc(adir: Path, turn: dict) -> dict:
    doc = {"kind": turn["kind"], "message": turn["message"],
           "thread": [{"role": r, "text": t[k]} for t in turn.get("thread", [])
                      for r, k in (("user", "message"), ("assistant", "text"))]}
    if turn["kind"] == "question":
        doc["question"] = turn.get("question")
        doc.update(_submitted(adir, doc["question"]))
    return doc


def _submitted(adir: Path, q: str) -> dict:
    """The attempt's side of a question once it is submitted (answers.json is written only then):
    {"submitted": the answer as text}, and once graded its "points" and, for a missed free-text
    one, its "feedback"; {} before submit."""
    path = adir / "answers.json"
    if not path.exists():
        return {}
    return {"submitted": _answer_text(adir, q, json.loads(path.read_text()).get(q)), **_graded(adir, q)}


def _answer_text(adir: Path, q: str, answer) -> str:
    """A submitted answer as `context` takes it, a string: a text answer as written, a choice
    answer as its option, and a bypassed question's missing answer said so."""
    if isinstance(answer, str):
        return answer
    if answer is None:
        return "(no answer: the question was bypassed)"
    options = _options(adir, q)
    return f"option {answer}: {options[answer]}" if 0 <= answer < len(options) else f"option {answer}"


def _options(adir: Path, q: str) -> list[str]:
    exam = json.loads((adir / "exam.json").read_text())
    return next((x.get("options") or [] for x in exam["questions"] if x["id"] == q), [])


def _graded(adir: Path, q: str) -> dict:
    path = adir / "result.json"
    got = next((r for r in json.loads(path.read_text())["questions"] if r["id"] == q), {}) if path.exists() else {}
    return {k: got[k] for k in ("points", "feedback") if got.get(k) not in (None, "")}


def _text_or_empty(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def session(cfg, *, run=subprocess.run):
    """`run(request, model=, timeout=)` of `exams.work`: one headless container whose
    only mounts are the job dir, the records (read-only), the skill and the model's auth."""
    def go(request: Path, *, model: str, timeout: int) -> None:
        job = request.parent
        spec = json.loads(request.read_text())
        prompt = job / "prompt.md"
        text = GRADE_PROMPT if spec.get("mode") == "grade" else PROMPT
        prompt.write_text(text.format(skill="/skill", request=request))
        name = f"exam-{job.parent.parent.name}-{job.parent.name}"
        cmd = containers.exam_cmd(name, str(job), spec.get("records"), str(SCRIPT.parent),
                                  cfg.session_memory, cfg.session_cpus, model, str(prompt))
        try:
            run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        except subprocess.TimeoutExpired:
            try:
                run(["podman", "kill", name], capture_output=True, text=True, timeout=60)
            except Exception:
                pass
            raise
    return go


def tick(cfg, deps, usages, work) -> None:
    """One round of `work` (`exams.work` or `exams.sweep`), outside task capacity. No App
    registered: nothing runs."""
    if not deps.app.registered():
        return
    work(cfg, github=Github(deps.access, deps.app), run=session(cfg), pr_exam=PrExam(),
         notify=deps.notifier.send, now=deps.clock, usages=usages)


def _running() -> bool:
    tab = herdr.Tab.find(TAB)
    return tab is not None and tab.alive


def _launch_env() -> dict[str, str]:
    """The runner tab's env: triage's, plus LAUNCH_ENV_VARS that are set."""
    return {**triage._launch_env(),
            **{var: os.environ[var] for var in LAUNCH_ENV_VARS if os.environ.get(var)}}


def launch(cfg, config_path: str) -> None:
    """Once per pass: start the runner in its herdr tab when an exam has work. The pass
    never waits on an exam session; the runner outlives the pass's oneshot unit."""
    if not exams.pending(cfg.state_dir) or _running():
        return
    tab = herdr.Tab.ensure(herdr.SYSTEM_WORKSPACE, TAB, triage._repo_dir(), env=_launch_env())
    if tab is None or not tab.run(f"{shlex.quote(sys.executable)} -m dispatcher.main "
                                  f"--config {shlex.quote(config_path)} --exam-run; exit"):
        print("exam runner launch failed: herdr tab could not run it", file=sys.stderr)


def _round(cfg, deps, work) -> None:
    """Runners never overlap: a second exits at once."""
    try:
        with state_lock(cfg.state_dir, RUN_LOCK, timeout=0):
            tick(cfg, deps, fetch_all(cfg), work)
    except TimeoutError:
        print("exam runner already running", file=sys.stderr)


def run(cfg, deps) -> None:
    """The runner's one round of exam work."""
    _round(cfg, deps, exams.work)


def sweep(cfg, deps) -> None:
    """The daily sweep: every exam brought in line with its PR, then the usual work."""
    _round(cfg, deps, exams.sweep)
