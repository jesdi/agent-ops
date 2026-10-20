"""Shared non-destructive board requirements for proposal and trusted control."""
from dispatcher.config import normalize_repo

_REQUIRED_FIELDS = (("Status", "single_select", ("Inbox", "Ready", "In progress")),
                    ("Impact", "number", ()), ("Effort", "number", ()),
                    ("Score", "number", ()), ("Area", "single_select", ("general",)))


def _check_board(state, members):
    """Why a board cannot serve these repositories; only what is missing is proposed, never a change."""
    problems = []
    unresolved = [c for c in state["cards"] if not c.get("repo") or not c.get("number")]
    if unresolved:
        problems.append("board contains cards with unresolved repository-qualified identity")
    outsiders = sorted({normalize_repo(c["repo"]) for c in state["cards"] if c.get("repo")} - set(members))
    problems += [f"board holds cards of {r}, which is not a member of this project" for r in outsiders]
    fields = {}
    for f in state["fields"]:
        fields.setdefault(f["name"], []).append(f)
    fix, options = [], []
    for name, kind, wanted in _REQUIRED_FIELDS:
        found = fields.get(name, [])
        if len(found) > 1:
            problems.append(f"field {name!r} is ambiguous: {len(found)} fields share that name")
        elif not found:
            fix.append({"name": name, "type": kind, **({"options": list(wanted)} if wanted else {})})
        elif found[0]["type"] != kind:
            problems.append(f"field {name!r} is {found[0]['type']}, required {kind}")
        else:
            have = found[0].get("options", [])
            # Existing Area choices are repository-specific and stay authoritative.
            required = () if name == "Area" and have else wanted
            options += [{"field": name, "name": o} for o in required if o not in have]
    return problems, fix, options

