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
    """GitHub or 1Password gave no clear answer: 401, 5xx, a timeout, an unreadable item."""


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
    except (ValueError, TypeError, AttributeError):
        raise AppError("the App private key is unreadable") from None


def _call(http, method, url, token, body=None, ok=(200,)):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        status, payload = http(method, url, headers, body)
    except TimeoutError:
        raise AppError(f"{method} {url} timed out") from None
    if status in ok or status == 404:
        return status, payload
    raise AppError(f"{method} {url} answered {status}")


def _installation(payload: dict) -> Installation:
    login, kind = payload["account"]["login"], payload["account"]["type"]
    base = (f"https://github.com/organizations/{login}/settings/installations"
            if kind == "Organization" else "https://github.com/settings/installations")
    return Installation(payload["id"], f"{base}/{payload['id']}", login)


def _lookup(path, http, now, sh):
    status, payload = _call(http, "GET", API + path, _jwt(now, sh))
    return None if status == 404 else _installation(payload)


def installation(repo: str, *, http=_http, now=_now, sh=_sh) -> Installation | None:
    """None means the App does not reach `repo` (GitHub answered 404)."""
    return _lookup(f"/repos/{repo}/installation", http, now, sh)


def mint(installation: Installation, repos: list[str], permissions: dict, *,
         http=_http, now=_now, sh=_sh) -> Token:
    names = [r.rsplit("/", 1)[-1] for r in repos]
    for r in repos:   # the request names repos without owner: refuse another owner's repo
        if r.split("/", 1)[0].lower() != installation.owner.lower():
            raise ValueError(f"{r} is not owned by the installation owner {installation.owner}")
    if not names:   # an empty list would make GitHub grant every repository
        raise ValueError("mint needs at least one repository")
    status, payload = _call(http, "POST", f"{API}/app/installations/{installation.id}/access_tokens",
                            _jwt(now, sh), {"repositories": names, "permissions": dict(permissions)},
                            ok=(201,))
    if status != 201:
        raise AppError(f"token request answered {status}")
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    return Token(payload["token"], expires)


def install_link(owner: str, *, http=_http, now=_now, sh=_sh) -> str:
    found = (_lookup(f"/users/{owner}/installation", http, now, sh)
             or _lookup(f"/orgs/{owner}/installation", http, now, sh))
    if found:
        return found.settings_url
    _, app = _call(http, "GET", API + "/app", _jwt(now, sh))
    return f"https://github.com/apps/{app['slug']}/installations/new"


def main(argv: list[str], *, http=_http, now=_now, sh=_sh) -> int:
    if len(argv) != 2 or argv[0] != "check" or "/" not in argv[1]:
        print("usage: python -m dispatcher.github_app check <owner/repo>")
        return 2
    repo = argv[1]
    kw = dict(http=http, now=now, sh=sh)
    try:
        _jwt(now, sh)
        print("jwt: passed")
        inst = installation(repo, **kw)
        if inst is None:
            print(f"installation: not reached; install the App here: "
                  f"{install_link(repo.split('/')[0], **kw)}")
            return 1
        print(f"installation: passed ({inst.id})")
        tok = mint(inst, [repo], {"contents": "read"}, **kw)
        print(f"token: passed (expires {tok.expires_at.isoformat()})")
        status, _ = _call(http, "GET", f"{API}/repos/{repo}", tok.token)
        if status != 200:
            print("repository read: failed")
            return 1
        print("repository read: passed")
        return 0
    except AppError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
