"""`agent-ops project status <name>`: a read of authorization and write verification."""
import re

# App keys, board credentials and infrastructure credentials never belong in a task environment.
_PRIVATE_ENV = re.compile(r"APP_|PRIVATE_KEY|BOARD|INFRA", re.IGNORECASE)


BOARD_WRITE = "update project board"  # required evidence: the project board write


def show(value):
    """Box-supplied text, escaped when it holds control characters that could spoof the prompt."""
    text = str(value)
    return text if text.isprintable() else repr(text)


def _violation(identity):
    if identity["verification"] == identity["task"]:
        return "verification identity equals the task identity"
    leaked = [n for n in identity["task_env"] if _PRIVATE_ENV.search(n)]
    return f"task environment carries private credentials: {', '.join(map(show, leaked))}" if leaked else None


def _section(title, lines, out):
    if lines:
        print(f"{title}\n" + "\n".join(f"  {l}" for l in lines) + "\n", file=out)


def status(args, out, err, box):
    state = box.status(args[0]) if args else None
    if state is None:
        print(f"unknown project {args[0] if args else ''!r}", file=err)
        return 1
    problem = _violation(state["identity"])
    if problem:
        print(f"identity violation: {problem}", file=err)
        return 1
    repos = state["repos"]
    auth = [f"{show(r)}: {show(s['authorization']['state'])}"
            + (f" - {show(s['authorization']['action'])}" if s["authorization"]["action"] else "")
            for r, s in repos.items()]
    verify, cleanup = [], []
    for r, s in repos.items():
        v = s["verification"]
        verify.append(f"{show(r)}: {show(v['state'])}" + (f" - denied: {', '.join(map(show, v['denied']))}" if v["denied"] else ""))
        cleanup += [f"{show(r)}: {show(c['id'])} {show(c['state'])}" + (f" - {show(c['detail'])}" if c["detail"] else "")
                    for c in v["cleanup"]]
    caps = [f"{show(n)}: " + ("documented, " if c["documented"] else "not documented, ") + ("blocked (input missing)" if c["input"] == "missing"
            else "effective" if c["effective"] else "not effective")
            for n, c in state["capabilities"].items()]
    for title, lines in (("Authorization:", auth), ("Write verification:", verify),
                         ("Cleanup:", cleanup), ("Capabilities:", caps)):
        _section(title, lines, out)
    members = set(box.projects().get(args[0], ()))
    board = state["capabilities"].get(BOARD_WRITE)
    done = (bool(members) and bool(board) and board["effective"] and board["input"] == "present"
            and members <= set(repos)
            and all(s["authorization"]["state"] == "granted" and s["verification"]["state"] == "passed"
                for s in repos.values())
            and all(c["input"] == "present" and c["effective"] for c in state["capabilities"].values()))
    print("Onboarding: " + ("complete" if done else "incomplete"), file=out)
    return 0
