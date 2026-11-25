"""A fake `sh` for jesdi/agent-ops-infra: git (a scratch clone) and gh (pull requests, ci,
merge, close), with the state a test needs to assert on. Unrouted local commands answer "".

Interface defined by the services-recorder tests: the services config is
`config/services.yaml` on infra main; every gh/git call needs GH_TOKEN == TOKEN in its env
(else it fails); `down` makes every call fail like an unreachable GitHub."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

SERVICES = "config/services.yaml"
TOKEN = "infra-tok"
PR_URL = "https://github.com/jesdi/agent-ops-infra/pull/{}"


def failure(stderr):
    return subprocess.CalledProcessError(1, ["x"], output="", stderr=stderr)


class FakeInfra:
    def __init__(self, services: dict):
        self.main = {SERVICES: yaml.safe_dump(services), "config/targets.yaml": "projects: []\n"}
        self.down = False
        self.rogue = None         # {path: text}: a foreign PR found on whatever branch is asked for
        self.heads = {}           # branch -> pushed head sha
        self.snapshots = {}       # head sha -> {path: text}
        self.prs = {}             # number -> dict(number, branch, head, state, files, ci)
        self.created, self.pushes, self.merged, self.closed = [], [], [], []
        self.calls = []
        self._next = 12
        self._main_version = 1

    # -- test-side controls ---------------------------------------------------
    def ci(self, number, conclusion):
        self.prs[number]["ci"] = conclusion

    def deploy(self, state_dir):
        """The deploy sync installs infra main's services config on the box."""
        Path(state_dir, "services.yaml").write_text(self.main[SERVICES])

    def main_config(self):
        return yaml.safe_load(self.main[SERVICES])

    def gh_calls(self, key):
        return [c for c in self.calls if key in " ".join(c)]

    # -- the shell --------------------------------------------------------------
    def __call__(self, argv, cwd=None, env=None, **kw):
        self.calls.append(list(argv))
        line = " ".join(argv)
        if self.down:
            raise failure("could not resolve host: github.com")
        if "gh" in argv[:1] or "git" in argv[:1]:
            if (env or {}).get("GH_TOKEN") != TOKEN:
                raise failure("authentication required")
        if argv[0] == "gh":
            return self._gh(argv, line)
        return self._git(argv, line)

    def _scratch(self, argv):
        return Path(argv[argv.index("-C") + 1]) if "-C" in argv else None

    def _snapshot(self, scratch):
        return {str(p.relative_to(scratch)): p.read_text() for p in scratch.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(scratch).parts}

    def _git(self, argv, line):
        scratch = self._scratch(argv)
        if " clone " in line:
            Path(argv[-1], ".git").mkdir(parents=True)
            return ""
        if re.search(r"show origin/main:(\S+)", line):
            path = re.search(r"show origin/main:(\S+)", line).group(1)
            if path not in self.main:
                raise failure(f"fatal: path '{path}' does not exist")
            return self.main[path]
        if any(w in argv for w in ("checkout", "switch", "reset")) and scratch:
            for path, text in self.main.items():
                (scratch / path).parent.mkdir(parents=True, exist_ok=True)
                (scratch / path).write_text(text)
            return ""
        if "rev-parse" in argv and "HEAD" in argv and scratch:
            return self._sha(self._snapshot(scratch)) + "\n"
        if "rev-parse" in argv and "origin/main" in line:
            return f"base{self._main_version}\n"
        if " push " in line:
            return self._push(argv, line, scratch)
        for branch, head in self.heads.items():
            if branch in line and any(w in line for w in ("for-each-ref", "ls-remote", "rev-parse")):
                return head + "\n"
        return ""

    def _sha(self, files):
        return "h" + hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()[:8]

    def _push(self, argv, line, scratch):
        branch = re.search(r"HEAD:refs/heads/(\S+)", line).group(1)
        lease = re.search(r"--force-with-lease=([^:\s]+):(\S*)", line)
        if lease and lease.group(2) != self.heads.get(branch, ""):
            raise failure("stale info")
        files = self._snapshot(scratch)
        head = self._sha(files)
        self.heads[branch] = head
        self.snapshots[head] = files
        self.pushes.append((branch, head, self._diff(files)))
        for pr in self.prs.values():  # a push moves the open PR's head; ci starts over
            if pr["branch"] == branch and pr["state"] == "OPEN" and pr["head"] != head:
                pr.update(head=head, files=self._diff(files), ci=None)
        return ""

    def _diff(self, files):
        return sorted(p for p, t in files.items() if self.main.get(p) != t)

    # -- gh -------------------------------------------------------------------------
    def _gh(self, argv, line):
        number = re.search(r"(?:pr (?:view|merge|close|diff)|pulls)[ /](\d+)", line)
        number = int(number.group(1)) if number else None
        if "pr list" in line:
            return self._list(argv)
        if "pr create" in line:
            return self._create(argv)
        if "pr merge" in line or ("pulls" in line and line.rstrip().endswith("merge")):
            return self._merge(number)
        if "pr close" in line or "state=closed" in line:
            self.prs[number]["state"] = "CLOSED"
            self.closed.append(number)
            return ""
        if "pr view" in line or "pr diff" in line or re.search(r"pulls/\d+(/files)?$", line):
            return self._view(number, line)
        if "actions/runs?head_sha=" in line:
            head = re.search(r"head_sha=(\w+)", line).group(1)
            ci = next((p["ci"] for p in self.prs.values() if p["head"] == head), None)
            runs = [] if ci is None else [{"id": 1, "name": "ci", "status": "completed",
                    "conclusion": ci, "html_url": "https://github.com/jesdi/agent-ops-infra/actions/runs/1"}]
            return json.dumps({"workflow_runs": runs})
        if re.search(r"commits/\w+/status", line):
            return json.dumps({"statuses": []})
        return ""

    def _row(self, p):
        return {"number": p["number"], "url": PR_URL.format(p["number"]), "headRefOid": p["head"],
                "headRefName": p["branch"], "state": p["state"], "baseRefName": "main",
                "mergedAt": "2026-11-25T10:00:00Z" if p["state"] == "MERGED" else None,
                "mergeCommit": {"oid": "merge1"} if p["state"] == "MERGED" else None,
                "files": [{"path": f} for f in p["files"]]}

    def _list(self, argv):
        branch = argv[argv.index("--head") + 1] if "--head" in argv else None
        if self.rogue and branch and not any(p["branch"] == branch for p in self.prs.values()):
            files = {**self.main, **self.rogue}
            head = self._sha(files)
            self.snapshots[head] = files
            self.prs[99] = dict(number=99, branch=branch, head=head, state="OPEN",
                                files=self._diff(files), ci="success")
        wanted = "open" if "open" in argv else None
        return json.dumps([self._row(p) for p in self.prs.values()
                           if p["branch"] == branch and (wanted is None or p["state"] == "OPEN")])

    def _create(self, argv):
        branch = argv[argv.index("--head") + 1]
        head = self.heads[branch]
        n, self._next = self._next, self._next + 1
        self.prs[n] = dict(number=n, branch=branch, head=head, state="OPEN",
                           files=self._diff(self.snapshots[head]), ci=None)
        self.created.append(n)
        return PR_URL.format(n) + "\n"

    def _view(self, number, line):
        p = self.prs[number]
        if "--name-only" in line:
            return "\n".join(p["files"]) + "\n"
        if "/files" in line:
            return json.dumps([{"filename": f} for f in p["files"]])
        return json.dumps(self._row(p))

    def _merge(self, number):
        p = self.prs[number]
        self.merged.append((number, p["head"], p["ci"], list(p["files"])))
        p["state"] = "MERGED"
        self.main = dict(self.snapshots[p["head"]])
        self._main_version += 1
        return ""
