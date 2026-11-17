"""Short builders of a stored status (dispatcher.status), shared by the suite."""
from dispatcher.state import AnswersRequest, PlanApprovalRequest
from dispatcher.status import Cause, FailReason, HumanReason, Kind, Status

RUNNING = Status(Kind.RUNNING)
PR = Status(Kind.PARKED_PR)
CANCELED = Status(Kind.CANCELED)
DONE = Status(Kind.DONE)


def human(reason: str = "operator", note: str = "") -> Status:
    return Status(Kind.PARKED_HUMAN, reason=HumanReason(reason), note=note)


def answers(request: AnswersRequest | str, note: str = "") -> Status:
    """Parked for the answers page `request` (a request, or a page path)."""
    path, fingerprint = _page(request)
    return Status(Kind.PARKED_ANSWERS, path=path, fingerprint=fingerprint, note=note)


def held(request: PlanApprovalRequest | str = ".agent/review.html") -> Status:
    """Held at the plan gate on `request` (a request, or a page path)."""
    path, fingerprint = _page(request)
    return Status(Kind.HELD_PLAN_APPROVAL, path=path, fingerprint=fingerprint)


def review(request: PlanApprovalRequest | str = ".agent/review.html", note: str = "") -> Status:
    """Parked for review on `request` (a request, or a page path)."""
    path, fingerprint = _page(request)
    return Status(Kind.PARKED_REVIEW, path=path, fingerprint=fingerprint, note=note)


def _page(request) -> tuple[str, str]:
    if isinstance(request, str):
        return request, ""
    return request.path, request.fingerprint


def login(note: str = "") -> Status:
    return Status(Kind.HELD_LOGIN, note=note)


def access(note: str = "") -> Status:
    return Status(Kind.PARKED_ACCESS, note=note)


def ci(run_id: int) -> Status:
    return Status(Kind.PARKED_CI, run_id=run_id)


def waking(origin: Status | None = None, cause: str = "reply") -> Status:
    """Woken from `origin` for `cause`; a parked-for-the-operator task when
    no origin is given, but for a launch or the next stage."""
    if origin is None and cause not in ("launch", "next-stage"):
        origin = human()
    return Status(Kind.WAKING, origin=origin, cause=Cause(cause))


def failed(reason: str = "session-crash") -> Status:
    return Status(Kind.FAILED, reason=FailReason(reason))
