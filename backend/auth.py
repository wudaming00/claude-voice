"""Shared-secret password auth + per-IP brute-force protection.

Why this exists
---------------
Claude Voice usually runs behind a Tailscale Funnel or similar tunnel that
exposes the server to the public internet (because phones need HTTPS to
open the mic). Without auth, anyone who learned the URL could drive
``claude`` inside the configured ``CLAUDE_CWD``. Bad day.

This module adds the smallest safety net that still deserves the name:

* A single shared password from ``AUTH_PASSWORD`` in ``.env``. Empty
  means auth is disabled — appropriate when the server is only reachable
  over a trusted network (e.g. Tailscale Serve on a tailnet, LAN only).
* Stateless HMAC-signed bearer tokens. The server holds no session
  table, so a restart does NOT log users out, and rotating the password
  automatically invalidates every token in the world (because the HMAC
  key is derived from the password). Tokens carry their own expiry in
  plaintext so the front-end can schedule refresh without a server round
  trip.
* Per-IP rate limiting: after ``max_failures`` wrong passwords inside
  ``window_seconds``, that IP is locked out for ``lockout_seconds``.

Threat model: a stranger who finds the URL and tries dictionary / common
passwords. We defeat them by making guesses slow (lockout) and requiring
a high-entropy password (``setup.sh`` generates one). We do NOT attempt
to defeat a targeted attacker with a compromised device; defense in
depth for that is out of scope for a personal tool.

Token format
------------
``<expires_at_epoch>.<hex_hmac>`` where ``hex_hmac`` is the first 32 hex
chars of ``HMAC-SHA256(secret, expires_at_epoch)``. The secret is
``SHA-256(AUTH_PASSWORD)``, so a password rotation re-derives a fresh
secret and every previously issued token fails verification on its next
use. We truncate to 128 bits, which is plenty for this threat model and
keeps URLs / cookies short.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional


class AuthConfig:
    def __init__(self) -> None:
        self.password: str = (os.environ.get("AUTH_PASSWORD") or "").strip()
        self.enabled: bool = bool(self.password)
        # Tuned for personal use: a real user mistypes 1–2 times, anyone
        # past 5 is almost certainly automated.
        self.max_failures: int = 5
        self.window_seconds: int = 15 * 60
        self.lockout_seconds: int = 15 * 60
        # 30 days. Balances UX (rarely see the login screen) against the
        # blast radius if a device is compromised (max 30 days of access
        # without the password). Clients refresh when <7 days remain.
        self.token_ttl_seconds: int = 30 * 24 * 3600
        # Refresh eligibility: any token with a valid signature AND still
        # within its TTL can be refreshed. Expired tokens cannot — the
        # user has to log in again.
        self.refresh_window_seconds: int = 7 * 24 * 3600


@dataclass
class _IPState:
    failures: Deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class Auth:
    def __init__(self, cfg: AuthConfig) -> None:
        self.cfg = cfg
        self._ip_state: dict[str, _IPState] = defaultdict(_IPState)
        # HMAC secret derived from the password. Storing the derived bytes
        # (not the password) avoids accidentally surfacing the password in
        # stack traces, debug logs, or REPL prints.
        self._secret: bytes = hashlib.sha256(self.cfg.password.encode("utf-8")).digest()

    # ---- Tokens -----------------------------------------------------------
    def _sign(self, expires_at: int) -> str:
        payload = str(expires_at).encode("ascii")
        mac = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        # 128 bits of HMAC is plenty for this threat model; truncate for
        # shorter URLs / cookies.
        return f"{expires_at}.{mac[:32]}"

    def _issue_token(self, ttl_seconds: Optional[int] = None) -> str:
        ttl = ttl_seconds if ttl_seconds is not None else self.cfg.token_ttl_seconds
        expires_at = int(time.time()) + ttl
        return self._sign(expires_at)

    @staticmethod
    def _parse(token: str) -> Optional[tuple[int, str]]:
        try:
            exp_str, mac = token.split(".", 1)
            return int(exp_str), mac
        except (ValueError, AttributeError):
            return None

    def _verify_signature(self, token: str) -> Optional[int]:
        """Returns the expiry epoch if signature is valid, else None."""
        parsed = self._parse(token)
        if parsed is None:
            return None
        expires_at, _mac = parsed
        expected = self._sign(expires_at)
        # Constant-time compare over the full token string to resist
        # timing attacks (though a personal tool over HTTPS is a stretch
        # of a threat model, good hygiene is cheap).
        if not hmac.compare_digest(expected, token):
            return None
        return expires_at

    def validate_token(self, token: Optional[str]) -> bool:
        if not self.cfg.enabled:
            return True
        if not token:
            return False
        expires_at = self._verify_signature(token)
        if expires_at is None:
            return False
        return expires_at > time.time()

    def refresh_token(self, token: Optional[str]) -> Optional[str]:
        """Issue a fresh token in exchange for a still-valid one.

        Returns the new token on success or None if the old token is
        missing, forged, or expired. Expired tokens cannot be refreshed
        — that's the 30-day ceiling at work. The user has to re-log-in
        with the password (or rescan the QR) to get moving again.
        """
        if not self.cfg.enabled:
            return self._issue_token()
        if not token:
            return None
        expires_at = self._verify_signature(token)
        if expires_at is None or expires_at <= time.time():
            return None
        return self._issue_token()

    # ---- Login ------------------------------------------------------------
    def login(self, ip: str, password: str) -> tuple[Optional[str], Optional[str]]:
        """Try to exchange a password for a token.

        Returns ``(token, None)`` on success or ``(None, error_message)`` on
        failure. The error message is safe to show to the user; it never
        reveals whether the password was close or what the real password is.
        """
        if not self.cfg.enabled:
            # Auth disabled: hand out a token so the client has something
            # to carry; ``validate_token`` ignores it anyway.
            return self._issue_token(), None

        now = time.time()
        state = self._ip_state[ip]

        # Evict failure timestamps older than the sliding window before we
        # decide whether the caller is past the threshold.
        cutoff = now - self.cfg.window_seconds
        while state.failures and state.failures[0] < cutoff:
            state.failures.popleft()

        if state.locked_until > now:
            wait = int(state.locked_until - now)
            return None, f"Too many failed attempts. Try again in {wait}s."

        if hmac.compare_digest((password or "").encode(), self.cfg.password.encode()):
            # Wipe the failure history so a user who fat-fingered once or
            # twice before succeeding doesn't stay shadow-penalised.
            state.failures.clear()
            return self._issue_token(), None

        state.failures.append(now)
        if len(state.failures) >= self.cfg.max_failures:
            state.locked_until = now + self.cfg.lockout_seconds
            state.failures.clear()
            mins = self.cfg.lockout_seconds // 60
            return None, f"Too many failed attempts. Locked out for {mins} minutes."
        remaining = self.cfg.max_failures - len(state.failures)
        return None, f"Wrong password. {remaining} attempt(s) left before lockout."


def client_ip(req: Any) -> str:
    """Best-effort peer IP for both HTTP ``Request`` and WebSocket objects.

    Starlette exposes ``.client.host`` on both. When behind a reverse proxy
    (Tailscale Funnel, nginx, Caddy) we prefer ``X-Forwarded-For`` because
    otherwise every request looks like it's from 127.0.0.1 and a single
    attacker gets the whole shared lockout budget.
    """
    try:
        xff = req.headers.get("x-forwarded-for") or ""
    except Exception:
        xff = ""
    if xff:
        return xff.split(",")[0].strip()
    try:
        return (req.client.host if req.client else None) or "unknown"
    except Exception:
        return "unknown"
