"""GitHub App access: sign as the App, find installations, mint repo-narrowed tokens.

The private key is read from 1Password on every call, held in memory only, and
never written, logged, put in argv or in an exception message.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

Http = Callable[..., tuple[int, dict]]   # (method, url, headers, body) -> (status, payload)

API = "https://api.github.com"
ITEM = "op://agent-ops/agent-ops-github-app/"
TIMEOUT = 20


class AppError(Exception):
    """GitHub or 1Password gave no clear answer: 401, 5xx, a timeout, an unreadable item,
    a body without the expected fields, a key that can't sign."""


@dataclass(frozen=True)
class Installation:
    id: int
    settings_url: str
    owner: str


@dataclass(frozen=True)
class Token:
    token: str
    expires_at: datetime


def _http(method, url, headers, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:   # socket.timeout is an OSError
        raise TimeoutError(type(exc).__name__) from None
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


def _sh(argv, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


def _now():
    return datetime.now(timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _read(sh, field: str) -> str:
    try:
        return sh(["op", "read", ITEM + field]).strip()
    except (subprocess.SubprocessError, OSError):   # never carry stderr: it may name the item
        raise AppError(f"cannot read {field} from 1Password") from None


def _jwt(now, sh) -> str:
    app_id, pem = _read(sh, "GITHUB_APP_ID"), _read(sh, "GITHUB_APP_PRIVATE_KEY")
    try:
        key = serialization.load_pem_private_key(pem.encode(), None)
        iat = int((now() - timedelta(minutes=1)).timestamp())
        signing = ".".join(_b64(json.dumps(p, separators=(",", ":")).encode()) for p in (
            {"alg": "RS256", "typ": "JWT"},
            {"iss": app_id, "iat": iat, "exp": iat + 9 * 60}))
        return signing + "." + _b64(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))
    except Exception:   # any load or sign failure, UnsupportedAlgorithm included; never the key
        raise AppError("the App private key is unreadable") from None


def _call(http, method, url, token, read, body=None, ok=200):
    """`read(payload)` on `ok`, None on 404, AppError on anything else: the one
    place a GitHub answer is read, so a body missing a field is an AppError too."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        status, payload = http(method, url, headers, body)
    except TimeoutError:
        raise AppError(f"{method} {url} timed out") from None
    if status == 404:
        return None
    if status != ok:
        raise AppError(f"{method} {url} answered {status}")
    try:
        return read(payload)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AppError(f"{method} {url} answered without the expected fields") from None


def _installation(payload: dict) -> Installation:
    login, kind = payload["account"]["login"], payload["account"]["type"]
    if not isinstance(login, str) or not login:
        raise ValueError("no account login")
    base = (f"https://github.com/organizations/{login}/settings/installations"
            if kind == "Organization" else "https://github.com/settings/installations")
    return Installation(int(payload["id"]), f"{base}/{payload['id']}", login)


def _token(payload: dict) -> Token:
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None or not isinstance(payload["token"], str) or not payload["token"]:
        raise ValueError("no token or a naive expiry")
    return Token(payload["token"], expires)


def _slug(payload: dict) -> str:
    if not isinstance(payload["slug"], str) or not payload["slug"]:
        raise ValueError("no slug")
    return payload["slug"]


def _split(repo: str) -> tuple[str, str]:
    """owner, name of `owner/name`; ValueError when either is empty."""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"{repo!r} is not owner/name")
    return owner, name


def _lookup(path, http, now, sh):
    return _call(http, "GET", API + path, _jwt(now, sh), _installation)


def installation(repo: str, *, http=_http, now=_now, sh=_sh) -> Installation | None:
    """None means the App does not reach `repo` (GitHub answered 404)."""
    return _lookup(f"/repos/{repo}/installation", http, now, sh)


def mint(installation: Installation, repos: list[str], permissions: dict, *,
         http=_http, now=_now, sh=_sh) -> Token:
    if not repos:   # an empty list would make GitHub grant every repository
        raise ValueError("mint needs at least one repository")
    names = []
    for owner, name in map(_split, repos):   # the request names repos without owner
        if owner.lower() != installation.owner.lower():
            raise ValueError(f"{owner}/{name} is not owned by the installation owner {installation.owner}")
        names.append(name)
    token = _call(http, "POST", f"{API}/app/installations/{installation.id}/access_tokens",
                  _jwt(now, sh), _token, {"repositories": names, "permissions": dict(permissions)},
                  ok=201)
    if token is None:
        raise AppError("token request answered 404")
    return token


def install_link(owner: str, *, http=_http, now=_now, sh=_sh) -> str:
    found = (_lookup(f"/users/{owner}/installation", http, now, sh)
             or _lookup(f"/orgs/{owner}/installation", http, now, sh))
    if found:
        return found.settings_url
    slug = _call(http, "GET", API + "/app", _jwt(now, sh), _slug)
    if slug is None:
        raise AppError("GET /app answered 404")
    return f"https://github.com/apps/{slug}/installations/new"


class NoAccess(Exception):
    """The App can't serve these repositories until the operator acts: it is not
    registered, or its installation doesn't reach one. `link` says where to act."""

    def __init__(self, detail: str, link: str):
        super().__init__(detail)
        self.link = link


README = "https://github.com/jesdi/agent-ops-infra/blob/main/README.md#github-app"
REFRESH = timedelta(minutes=5)   # remint a token this close to its expiry


class Client:
    """The App as onboarding sees it: the module's calls behind one object."""

    def __init__(self, *, http=None, now=None, sh=None):   # None: the module's, read now
        self.kw = dict(http=http or _http, now=now or _now, sh=sh or _sh)

    def registered(self) -> bool:
        """The 1Password item gives an App id and a private key that signs."""
        try:
            _jwt(self.kw["now"], self.kw["sh"])
        except AppError:
            return False
        return True

    def installation(self, repo):
        return installation(repo, **self.kw)

    def install_link(self, owner):
        return install_link(owner, **self.kw)

    def mint(self, inst, repos, permissions):
        return mint(inst, repos, permissions, **self.kw)


class Access:
    """An App's reach of repositories, and installation tokens for them cached
    until REFRESH before expiry. `app` is a Client or anything shaped like it."""

    def __init__(self, app, now: Callable[[], datetime] = _now):
        self.app, self.now, self._cache = app, now, {}

    def reached(self, repos: list[str]) -> dict[Installation, list[str]]:
        """The repos grouped by the installation that reaches them, asked fresh.
        NoAccess when the App is unregistered or misses one; AppError passes."""
        if not self.app.registered():
            raise NoAccess("the GitHub App is not registered: the 1Password item "
                           "agent-ops/agent-ops-github-app is unreadable; follow the "
                           "infra README checklist", README)
        by_install, missing = {}, []
        for repo in dict.fromkeys(repos):
            if inst := self.app.installation(repo):
                by_install.setdefault(inst, []).append(repo)
            else:
                missing.append(repo)
        if missing:
            raise NoAccess(f"the GitHub App can't reach {', '.join(missing)}: add it to "
                           "the App installation", self.app.install_link(_split(missing[0])[0]))
        return by_install

    def grants(self, repos: list[str], permissions: dict) -> dict[str, Token]:
        """Lowercased repo -> a Token for it: one mint per installation, naming
        only that installation's repos of `repos`."""
        if not repos:
            return {}
        key = (tuple(sorted({x.lower() for x in repos})), tuple(sorted(permissions.items())))
        if (hit := self._cache.get(key)) and min(t.expires_at for t in hit.values()) - REFRESH > self.now():
            return hit
        grants = {}
        for inst, names in self.reached(repos).items():
            token = self.app.mint(inst, names, permissions)
            grants.update({x.lower(): token for x in names})
        self._cache[key] = grants
        return grants

    def tokens(self, repos: list[str], permissions: dict) -> dict[str, str]:
        """Lowercased repo -> its token string (see grants)."""
        return {repo: t.token for repo, t in self.grants(repos, permissions).items()}


def _read_repo(http, repo: str, token: str) -> str:
    """'passed', or why the token couldn't read `repo`."""
    try:
        read = _call(http, "GET", f"{API}/repos/{repo}", token, lambda _: True)
    except AppError as exc:
        return f"failed ({exc})"
    return "passed" if read else "failed (404)"


def _check(repo: str, http, now, sh) -> int:
    kw = dict(http=http, now=now, sh=sh)
    _jwt(now, sh)
    print("jwt: passed")
    inst = installation(repo, **kw)
    if inst is None:
        print(f"installation: not reached; install the App here: "
              f"{install_link(_split(repo)[0], **kw)}")
        return 1
    print(f"installation: passed ({inst.id})")
    tok = mint(inst, [repo], {"contents": "read"}, **kw)
    print(f"token: passed (expires {tok.expires_at.isoformat()})")
    result = _read_repo(http, repo, tok.token)
    print(f"repository read: {result}")
    return 0 if result == "passed" else 1


def main(argv: list[str], *, http=_http, now=_now, sh=_sh) -> int:
    if len(argv) != 2 or argv[0] != "check" or argv[1].count("/") != 1 or "" in argv[1].split("/"):
        print("usage: python -m dispatcher.github_app check <owner/repo>")
        return 2
    try:
        return _check(argv[1], http, now, sh)
    except AppError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
