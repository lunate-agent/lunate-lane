"""GitHub App installation tokens for the lane's two identities.

A short-lived token is minted per run from the App's private key. The key is read, used, and never printed,
copied or logged; only the token exchange's outcome is reportable.

    python3 scripts/identity.py worker        # mint and make one harmless read
    python3 scripts/identity.py reviewer
"""

from __future__ import annotations

import base64
import json
import pathlib
import time
import urllib.error
import urllib.request

CONFIG = pathlib.Path.home() / ".config" / "lunate-lane" / "app.json"
API = "https://api.github.com"
REPO = "lunate-agent/lunate"


class IdentityError(RuntimeError):
    """Anything that stops a token being minted. Never carries key material."""


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def app_jwt(app_id: int, key_path: pathlib.Path) -> str:
    """A 9-minute RS256 JWT signed with the App's private key."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - dependency check
        raise IdentityError("the cryptography package is required") from exc
    if not key_path.exists():
        raise IdentityError(f"no private key at {key_path}")
    try:
        private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:
        raise IdentityError("the private key could not be read") from exc
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = header + b"." + payload
    signature = _b64(private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    return (signing_input + b"." + signature).decode()


def installation_token(name: str) -> str:
    """Mint an installation token. The token is returned, never printed."""
    config = json.loads(CONFIG.read_text())
    if name not in config:
        raise IdentityError(f"unknown identity {name!r}; known: {', '.join(sorted(config))}")
    app = config[name]
    jwt = app_jwt(int(app["app_id"]), pathlib.Path(app["key"]))
    request = urllib.request.Request(
        f"{API}/app/installations/{app['installation_id']}/access_tokens",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "lunate-lane",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["token"]
    except urllib.error.HTTPError as exc:
        raise IdentityError(f"the token exchange was refused ({exc.code})") from exc
    except Exception as exc:
        raise IdentityError(f"GitHub could not be reached ({exc.__class__.__name__})") from exc


def repo_read(token: str) -> dict:
    """One harmless read, to prove the token works."""
    request = urllib.request.Request(
        f"{API}/repos/{REPO}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "lunate-lane"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise IdentityError(f"the read was refused ({exc.code})") from exc


def check(name: str) -> str:
    """Report success or the error. Never the token."""
    try:
        token = installation_token(name)
    except IdentityError as exc:
        return f"{name}: FAILED — {exc}"
    try:
        data = repo_read(token)
    except IdentityError as exc:
        return f"{name}: token minted, read failed — {exc}"
    return f"{name}: OK — read {data['full_name']} (repo id {data['id']})"


if __name__ == "__main__":
    import sys

    who = sys.argv[1:] or ["worker", "reviewer"]
    for target in who:
        print(check(target))
