"""Pairing-token gate for devices that reach the server over the network.

The dashboard binds to every interface so a phone can sync over Wi-Fi, a
hotspot or Tailscale.  Without a gate, anyone on the same café network could
read the notes-derived question bank and write reviews.  The rule is:

* a request from loopback (the laptop itself, or a phone through
  ``adb reverse``) needs nothing;
* any other client must present the pairing token once, at
  ``/pair?token=...``; it then holds a long-lived HttpOnly cookie.

The token lives in a 0600 file next to the database and never leaves the
machine except inside the pairing link the laptop's own Sync dialog shows.
``RECALL_LAN_AUTH=0`` switches the gate off for a trusted network.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from . import db

COOKIE = "recall_token"
COOKIE_MAX_AGE = 365 * 86400
OPEN_PATHS = {"/api/health", "/pair"}
MAX_FAILURES = 8
FAILURE_WINDOW = 300.0
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters
_failures: dict[str, list[float]] = {}


def enabled() -> bool:
    """The gate is on unless explicitly disabled."""
    return os.environ.get("RECALL_LAN_AUTH", "1").strip().lower() not in ("0", "false", "off", "no")


def token_path() -> Path:
    """Token file location: explicit override, else beside the database."""
    override = os.environ.get("RECALL_TOKEN_PATH")
    return Path(override) if override else db.database_path().parent / "sync_token"


def token() -> str:
    """Read the pairing token, creating it (owner-only) on first use."""
    path = token_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 12:
            return value
    except OSError:
        pass
    value = "".join(secrets.choice(_ALPHABET) for _ in range(16))  # ~79 bits
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def is_local(host: str) -> bool:
    """True for loopback peers.

    A peer that is not an IP literal only occurs with in-process ASGI test
    transports; uvicorn always reports a socket address.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def forwarded(scope: dict) -> bool:
    """True when a reverse proxy (``tailscale serve``, nginx) relayed the request.

    Such a request arrives from loopback although the real client is remote,
    so it must not inherit the laptop's trust.
    """
    names = {key.decode("latin-1").lower() for key, _value in scope.get("headers") or []}
    return bool(names & {"x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login"})


def trusted(scope: dict) -> bool:
    """A direct loopback request: the laptop itself or ``adb reverse``."""
    peer = (scope.get("client") or ("", 0))[0]
    return is_local(peer) and not forwarded(scope)


def valid(candidate: str | None) -> bool:
    """Constant-time comparison against the stored token."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.strip().encode(), token().encode())


def locked_out(host: str, now: float | None = None) -> bool:
    """Too many wrong tokens from this address in the last five minutes."""
    moment = time.time() if now is None else now
    recent = [stamp for stamp in _failures.get(host, []) if moment - stamp < FAILURE_WINDOW]
    _failures[host] = recent
    return len(recent) >= MAX_FAILURES


def note_failure(host: str, now: float | None = None) -> None:
    """Remember a wrong token so guessing is rate limited."""
    _failures.setdefault(host, []).append(time.time() if now is None else now)
    if len(_failures) > 2000:  # bound memory under a scan
        _failures.clear()


def presented(scope: dict) -> str | None:
    """Token from the cookie, an Authorization bearer, or X-Recall-Token."""
    headers = {key.decode("latin-1").lower(): value.decode("latin-1")
               for key, value in scope.get("headers") or []}
    bearer = headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    if headers.get("x-recall-token"):
        return headers["x-recall-token"].strip()
    jar = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except Exception:
        return None
    return jar[COOKIE].value if COOKIE in jar else None


def query_token(scope: dict) -> str | None:
    """``?token=`` on the pairing request."""
    values = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("token")
    return values[0] if values else None


def cookie_header() -> str:
    """Set-Cookie value for a paired device."""
    return (f"{COOKIE}={token()}; Max-Age={COOKIE_MAX_AGE}; Path=/; "
            "HttpOnly; SameSite=Lax")


PAIR_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair this device</title>
<style>body{font-family:system-ui,sans-serif;background:#090f1b;color:#e7eefb;
display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}
form{background:#111c2e;border:1px solid #2a3a53;border-radius:12px;padding:24px;
max-width:360px;width:100%}h1{font-size:18px;margin:0 0 8px}p{color:#a3b4ce;
font-size:14px;line-height:1.5}input,button{width:100%;box-sizing:border-box;
font-size:16px;padding:12px;border-radius:8px;border:1px solid #2a3a53;margin-top:10px}
input{background:#090f1b;color:#e7eefb;letter-spacing:.08em}
button{background:#92b4ff;color:#07101f;font-weight:600;border:0}</style>
<form action="/pair" method="get"><h1>Pair this device</h1>
<p>On the laptop open <b>Sync &amp; deck</b> and copy the pairing code shown
there. You only do this once per device.</p>
<input name="token" autocomplete="off" autocapitalize="none" spellcheck="false"
 placeholder="pairing code" required><button>Pair</button>__MESSAGE__</form>"""


def pair_page(message: str = "") -> str:
    """Minimal self-contained pairing form (no static assets needed)."""
    note = f'<p style="color:#ff8c9b">{message}</p>' if message else ""
    return PAIR_PAGE.replace("__MESSAGE__", note)
