"""GitHub App access: sign as the App, find installations, mint repo-narrowed tokens.

The App id and private key come from the unit's environment, loaded from the
box's encrypted secret cache (never from 1Password per call: that spent its daily
read limit). The key is held in memory only, and never written, logged, put in
argv or in an exception message.
"""
from __future__ import annotations

import base64
import json
import os
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
TIMEOUT = 20


class AppError(Exception):
    """GitHub gave no clear answer: 401, 5xx, a timeout, a body without the
    expected fields."""


class Unregistered(AppError):
    """The secret cache has no App id or private key (the 1Password item is
    absent), or the key can't sign: the App is not registered (Client.registered)."""




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


def _secret(field: str) -> str:
    """The field as the unit loaded it from the secret cache; empty when absent."""
    return os.environ.get(field, "")


def _now():
    return datetime.now(timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _read(secret, field: str) -> str:
    value = secret(field).strip()
    if not value:
        raise Unregistered(f"no {field} in the secret cache")
    return value


def _jwt(now, secret) -> str:
    app_id, pem = _read(secret, "GITHUB_APP_ID"), _read(secret, "GITHUB_APP_PRIVATE_KEY")
    try:
        key = serialization.load_pem_private_key(pem.encode(), None)
        iat = int((now() - timedelta(minutes=1)).timestamp())
        signing = ".".join(_b64(json.dumps(p, separators=(",", ":")).encode()) for p in (
            {"alg": "RS256", "typ": "JWT"},
            {"iss": app_id, "iat": iat, "exp": iat + 9 * 60}))
        return signing + "." + _b64(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))
    except Exception:   # any load or sign failure, UnsupportedAlgorithm included; never the key
        raise Unregistered("the App private key is unreadable") from None


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


def _lookup(path, http, now, secret):
    return _call(http, "GET", API + path, _jwt(now, secret), _installation)


def installation(repo: str, *, http=_http, now=_now, secret=_secret) -> Installation | None:
    """None means the App does not reach `repo` (GitHub answered 404)."""
    return _lookup(f"/repos/{repo}/installation", http, now, secret)


def mint(installation: Installation, repos: list[str], permissions: dict, *,
         http=_http, now=_now, secret=_secret) -> Token:
    if not repos:   # an empty list would make GitHub grant every repository
        raise ValueError("mint needs at least one repository")
    names = []
    for owner, name in map(_split, repos):   # the request names repos without owner
        if owner.lower() != installation.owner.lower():
            raise ValueError(f"{owner}/{name} is not owned by the installation owner {installation.owner}")
        names.append(name)
    token = _call(http, "POST", f"{API}/app/installations/{installation.id}/access_tokens",
                  _jwt(now, secret), _token, {"repositories": names, "permissions": dict(permissions)},
                  ok=201)
    if token is None:
        raise AppError("token request answered 404")
    return token


def install_link(owner: str, *, http=_http, now=_now, secret=_secret) -> str:
    found = (_lookup(f"/users/{owner}/installation", http, now, secret)
             or _lookup(f"/orgs/{owner}/installation", http, now, secret))
    if found:
        return found.settings_url
    slug = _call(http, "GET", API + "/app", _jwt(now, secret), _slug)
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

    def __init__(self, *, http=None, now=None, secret=None):   # None: the module's, read now
        self.kw = dict(http=http or _http, now=now or _now, secret=secret or _secret)

    def registered(self) -> bool:
        """The secret cache gives an App id and a private key that signs."""
        try:
            _jwt(self.kw["now"], self.kw["secret"])
        except Unregistered:
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
    until `refresh` before expiry. `app` is a Client or anything shaped like it."""

    def __init__(self, app, now: Callable[[], datetime] = _now, refresh: timedelta = REFRESH):
        self.app, self.now, self.refresh, self._cache = app, now, refresh, {}

    def _reach(self, repos: list[str]) -> tuple[dict[Installation, list[str]], list[str]]:
        """The repos grouped by the installation that reaches them, and those none reaches."""
        by_install, missing = {}, []
        for repo in dict.fromkeys(repos):
            if inst := self.app.installation(repo):
                by_install.setdefault(inst, []).append(repo)
            else:
                missing.append(repo)
        return by_install, missing

    def reached(self, repos: list[str]) -> dict[Installation, list[str]]:
        """The repos grouped by the installation that reaches them, asked fresh.
        NoAccess when the App is unregistered or misses one; AppError passes."""
        if not self.app.registered():
            raise NoAccess("the GitHub App is not registered: the secret cache has no "
                           "App id or key from 1Password item agent-ops/agent-ops-github-app; "
                           "follow the infra README checklist", README)
        by_install, missing = self._reach(repos)
        if missing:
            raise NoAccess(f"the GitHub App can't reach {', '.join(missing)}: add it to "
                           "the App installation", self.app.install_link(_split(missing[0])[0]))
        return by_install

    def _installations(self, repos: list[str], reached_only: bool) -> dict[Installation, list[str]]:
        if not reached_only:
            return self.reached(repos)
        return self._reach(repos)[0] if self.app.registered() else {}

    def grants(self, repos: list[str], permissions: dict, *,
               reached_only: bool = False) -> dict[str, Token]:
        """Lowercased repo -> a Token for it: one mint per installation, naming
        only that installation's repos of `repos`. reached_only: a repo no
        installation reaches (every repo, with no App registered) is left out
        instead of raising NoAccess."""
        if not repos:
            return {}
        key = (tuple(sorted({x.lower() for x in repos})), tuple(sorted(permissions.items())),
               reached_only)
        hit = self._cache.get(key)
        if hit is not None and all(t.expires_at - self.refresh > self.now() for t in hit.values()):
            return hit
        grants = {}
        for inst, names in self._installations(repos, reached_only).items():
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


def _check(repo: str, http, now, secret) -> int:
    kw = dict(http=http, now=now, secret=secret)
    _jwt(now, secret)
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


def main(argv: list[str], *, http=_http, now=_now, secret=_secret) -> int:
    if len(argv) != 2 or argv[0] != "check" or argv[1].count("/") != 1 or "" in argv[1].split("/"):
        print("usage: python -m dispatcher.github_app check <owner/repo>")
        return 2
    try:
        return _check(argv[1], http, now, secret)
    except AppError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
