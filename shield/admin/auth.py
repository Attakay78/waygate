"""Auth backends and token management for ShieldAdmin.

Supports three auth configuration styles:

* ``None``                        — no authentication (open access)
* ``("admin", "pass")``           — single username / password pair
* ``[("admin", "pass"), …]``      — multiple username / password pairs
* ``MyAuthBackend()``             — custom :class:`ShieldAuthBackend` subclass

Tokens
------
:class:`TokenManager` issues HMAC-SHA256 signed tokens that encode
``username``, ``platform``, expiry, and a random nonce.  No database is
required for verification; revocations are tracked in-process.

Token format::

    <base64url-payload>.<hex-hmac-sha256-signature>

where payload is compact JSON: ``{"sub","exp","jti","plat"}``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from abc import ABC, abstractmethod
from typing import Union

# Public type alias accepted by :func:`make_auth_backend`.
AuthConfig = Union[
    None,
    tuple[str, str],
    list[tuple[str, str]],
    "ShieldAuthBackend",
]


class ShieldAuthBackend(ABC):
    """Abstract base for custom credential validators.

    Subclass this and implement :meth:`authenticate_user` to plug in any
    auth source (database, LDAP, OAuth introspection, …).

    Optionally override :meth:`fingerprint` to make tokens survive process
    restarts and only invalidate when your credential store actually changes.
    """

    @abstractmethod
    def authenticate_user(self, username: str, password: str) -> bool:
        """Return ``True`` when *username* / *password* are valid."""
        ...

    def fingerprint(self) -> str:
        """Return a string that changes when credentials change.

        The default returns the class name, which is stable across restarts
        but does **not** track runtime credential changes.  Override this to
        return a value derived from your credential store (e.g. a hash of
        all username/password pairs, or a DB schema version) so that
        credential changes automatically invalidate existing tokens::

            def fingerprint(self) -> str:
                # Example: hash all (username, hashed_password) pairs.
                material = "|".join(
                    f"{u}:{h}" for u, h in sorted(self._db.items())
                )
                return hashlib.sha256(material.encode()).hexdigest()[:16]
        """
        return type(self).__qualname__


class _SingleUserAuth(ShieldAuthBackend):
    """Auth backend for a single ``(username, password)`` pair."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    def authenticate_user(self, username: str, password: str) -> bool:
        return username == self._username and password == self._password


class _MultiUserAuth(ShieldAuthBackend):
    """Auth backend for multiple ``(username, password)`` pairs."""

    def __init__(self, credentials: list[tuple[str, str]]) -> None:
        self._creds: dict[str, str] = {u: p for u, p in credentials}

    def authenticate_user(self, username: str, password: str) -> bool:
        stored = self._creds.get(username)
        return stored is not None and stored == password


def auth_fingerprint(auth: AuthConfig) -> str:
    """Return a short deterministic hash of the auth credentials.

    Mixed into the token signing key so that any change to ``auth=`` (new
    users, changed passwords, or switching from one user to another)
    automatically invalidates all previously issued tokens — even when a
    stable ``secret_key`` is configured.

    The fingerprint is a truncated SHA-256 of the credential material, so
    it never exposes the passwords themselves.
    """
    if auth is None:
        return "open"
    if isinstance(auth, tuple):
        material = f"{auth[0]}:{auth[1]}"
    elif isinstance(auth, list):
        # Sort so order doesn't matter; separator chosen to avoid collisions.
        material = "|".join(f"{u}\x00{p}" for u, p in sorted(auth))
    else:
        # Custom backend: delegate to its fingerprint() method.
        material = auth.fingerprint()
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def make_auth_backend(auth: AuthConfig) -> ShieldAuthBackend | None:
    """Convert *auth* config into a :class:`ShieldAuthBackend` instance.

    Parameters
    ----------
    auth:
        One of:

        * ``None`` — no authentication
        * ``(username, password)`` — single user
        * ``[(username, password), …]`` — multiple users
        * :class:`ShieldAuthBackend` instance — custom implementation
    """
    if auth is None:
        return None
    if isinstance(auth, tuple):
        return _SingleUserAuth(auth[0], auth[1])
    if isinstance(auth, list):
        return _MultiUserAuth(auth)
    return auth


class TokenManager:
    """HMAC-SHA256 signed token manager.

    Creates and verifies self-contained tokens without a database.
    Revoked tokens are tracked in memory (reset on process restart).

    Token format: ``<base64url-payload>.<hex-signature>``

    Payload JSON keys: ``sub`` (username), ``exp`` (Unix timestamp),
    ``jti`` (random nonce for revocation), ``plat`` (``"cli"`` or
    ``"dashboard"``).
    """

    COOKIE_NAME = "shield_session"
    TOKEN_HEADER = "X-Shield-Token"

    def __init__(
        self,
        secret_key: str | None = None,
        expiry_seconds: int = 86400,
        auth_fingerprint: str = "",
    ) -> None:
        """
        Parameters
        ----------
        secret_key:
            HMAC signing key.  If ``None`` a random key is generated —
            tokens will be invalidated on process restart.
        expiry_seconds:
            Token lifetime in seconds.  Default: 86400 (24 hours).
        auth_fingerprint:
            Short hash of the current auth credentials (produced by
            :func:`auth_fingerprint`).  Mixed into the signing key so that
            any change to the auth config invalidates all existing tokens,
            even when *secret_key* is stable across restarts.
        """
        raw = secret_key or secrets.token_hex(32)
        # Mix the auth fingerprint into the key so credential changes
        # (new user, changed password, switched user) automatically
        # invalidate all previously issued tokens.
        if auth_fingerprint:
            raw = f"{raw}:{auth_fingerprint}"
        self._key: bytes = raw.encode()
        self._expiry = expiry_seconds
        self._revoked: set[str] = set()

    @property
    def expiry_seconds(self) -> int:
        """Configured token lifetime in seconds."""
        return self._expiry

    def create(self, username: str, platform: str = "cli") -> tuple[str, float]:
        """Issue a new signed token.

        Returns ``(token_string, expires_at_unix_timestamp)``.
        """
        expires_at = time.time() + self._expiry
        payload = {
            "sub": username,
            "exp": expires_at,
            "jti": secrets.token_hex(8),
            "plat": platform,
        }
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        sig = hmac.new(self._key, payload_b64.encode(), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{sig}", expires_at

    def verify(self, token: str) -> tuple[str, str] | None:
        """Verify *token* and return ``(username, platform)`` or ``None``.

        Returns ``None`` when the token is malformed, expired, or revoked.
        """
        if not token or token in self._revoked:
            return None
        try:
            payload_b64, sig = token.rsplit(".", 1)
            expected = hmac.new(self._key, payload_b64.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return None
            pad = 4 - len(payload_b64) % 4
            padded = payload_b64 + "=" * (pad % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if time.time() > payload["exp"]:
                return None
            return payload["sub"], payload.get("plat", "cli")
        except Exception:
            return None

    def revoke(self, token: str) -> None:
        """Mark *token* as revoked (in-memory; cleared on restart)."""
        self._revoked.add(token)

    def extract_token(self, token_header_value: str) -> str | None:
        """Pull raw token from the ``X-Shield-Token`` header value."""
        return token_header_value.strip() or None

    def extract_cookie(self, cookies: dict[str, str]) -> str | None:
        """Pull raw token from the session cookie dict."""
        return cookies.get(self.COOKIE_NAME)
