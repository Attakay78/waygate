"""Tests for waygate.admin.auth — TokenManager and auth backends."""

from __future__ import annotations

import time

from waygate.admin.auth import (
    TokenManager,
    WaygateAuthBackend,
    _MultiUserAuth,
    _SingleUserAuth,
    auth_fingerprint,
    make_auth_backend,
)

# ---------------------------------------------------------------------------
# make_auth_backend
# ---------------------------------------------------------------------------


def test_make_auth_backend_none_returns_none() -> None:
    assert make_auth_backend(None) is None


def test_make_auth_backend_single_tuple() -> None:
    backend = make_auth_backend(("admin", "pass"))
    assert isinstance(backend, _SingleUserAuth)
    assert backend.authenticate_user("admin", "pass") is True
    assert backend.authenticate_user("admin", "wrong") is False


def test_make_auth_backend_list_of_tuples() -> None:
    backend = make_auth_backend([("alice", "a1"), ("bob", "b2")])
    assert isinstance(backend, _MultiUserAuth)
    assert backend.authenticate_user("alice", "a1") is True
    assert backend.authenticate_user("bob", "b2") is True
    assert backend.authenticate_user("alice", "b2") is False


def test_make_auth_backend_custom_class() -> None:
    class MyAuth(WaygateAuthBackend):
        def authenticate_user(self, username: str, password: str) -> bool:
            return username == "su" and password == "root"

    custom = MyAuth()
    result = make_auth_backend(custom)
    assert result is custom
    assert result.authenticate_user("su", "root") is True


def test_custom_backend_default_fingerprint_uses_class_name() -> None:
    class MyAuth(WaygateAuthBackend):
        def authenticate_user(self, username: str, password: str) -> bool:
            return True

    backend = MyAuth()
    # Default fingerprint is the qualname — stable across restarts.
    assert backend.fingerprint() == MyAuth.__qualname__
    assert auth_fingerprint(backend) == auth_fingerprint(MyAuth())


def test_custom_backend_overridden_fingerprint_changes_with_credentials() -> None:
    """Custom backend that overrides fingerprint() invalidates tokens on cred change."""

    class MyAuth(WaygateAuthBackend):
        def __init__(self, users: dict) -> None:
            self._users = users

        def authenticate_user(self, username: str, password: str) -> bool:
            return self._users.get(username) == password

        def fingerprint(self) -> str:
            import hashlib

            material = "|".join(f"{u}:{p}" for u, p in sorted(self._users.items()))
            return hashlib.sha256(material.encode()).hexdigest()[:16]

    stable_key = "stable-key"
    old_backend = MyAuth({"admin": "pass"})
    new_backend = MyAuth({"kwame": "pass123"})

    tm_old = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(old_backend),
    )
    token, _ = tm_old.create("admin")

    tm_new = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(new_backend),
    )
    assert tm_new.verify(token) is None  # credentials changed → token invalid


def test_custom_backend_stable_fingerprint_survives_restart() -> None:
    """Custom backend with unchanged fingerprint keeps tokens valid across restarts."""

    class MyAuth(WaygateAuthBackend):
        def __init__(self, users: dict) -> None:
            self._users = users

        def authenticate_user(self, username: str, password: str) -> bool:
            return self._users.get(username) == password

        def fingerprint(self) -> str:
            import hashlib

            material = "|".join(f"{u}:{p}" for u, p in sorted(self._users.items()))
            return hashlib.sha256(material.encode()).hexdigest()[:16]

    stable_key = "stable-key"
    same_creds = {"admin": "pass"}

    tm_old = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(MyAuth(same_creds)),
    )
    token, _ = tm_old.create("admin")

    # Simulate restart: new backend instance, same credentials.
    tm_new = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(MyAuth(same_creds)),
    )
    result = tm_new.verify(token)
    assert result is not None
    assert result[0] == "admin"


# ---------------------------------------------------------------------------
# TokenManager — create / verify
# ---------------------------------------------------------------------------


def test_token_create_returns_string_and_expiry() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    token, expires_at = tm.create("alice")
    assert isinstance(token, str)
    assert "." in token
    assert expires_at > time.time()


def test_token_verify_valid_returns_username_and_platform() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    token, _ = tm.create("alice", platform="cli")
    result = tm.verify(token)
    assert result is not None
    assert result[0] == "alice"
    assert result[1] == "cli"


def test_token_verify_wrong_secret_returns_none() -> None:
    tm1 = TokenManager(secret_key="secret-a", expiry_seconds=3600)
    tm2 = TokenManager(secret_key="secret-b", expiry_seconds=3600)
    token, _ = tm1.create("alice")
    assert tm2.verify(token) is None


def test_token_verify_tampered_payload_returns_none() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    token, _ = tm.create("alice")
    # Flip a character in the payload section.
    payload_b64, sig = token.rsplit(".", 1)
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig
    assert tm.verify(tampered) is None


def test_token_verify_expired_returns_none() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=-1)  # already expired
    token, _ = tm.create("alice")
    assert tm.verify(token) is None


def test_token_verify_revoked_returns_none() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    token, _ = tm.create("alice")
    tm.revoke(token)
    assert tm.verify(token) is None


def test_token_verify_empty_string_returns_none() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    assert tm.verify("") is None


def test_token_verify_garbage_returns_none() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    assert tm.verify("not.a.valid.token.at.all") is None


def test_token_dashboard_platform() -> None:
    tm = TokenManager(secret_key="test-secret", expiry_seconds=3600)
    token, _ = tm.create("bob", platform="dashboard")
    result = tm.verify(token)
    assert result is not None
    assert result[1] == "dashboard"


# ---------------------------------------------------------------------------
# TokenManager — extract helpers
# ---------------------------------------------------------------------------


def test_extract_token_from_header() -> None:
    tm = TokenManager()
    assert tm.extract_token("mytoken") == "mytoken"
    assert tm.extract_token("  mytoken  ") == "mytoken"
    assert tm.extract_token("") is None


def test_extract_cookie() -> None:
    tm = TokenManager()
    assert tm.extract_cookie({"waygate_session": "tok123"}) == "tok123"
    assert tm.extract_cookie({}) is None
    assert tm.extract_cookie({"other": "val"}) is None


# ---------------------------------------------------------------------------
# expiry_seconds property
# ---------------------------------------------------------------------------


def test_expiry_seconds_property() -> None:
    tm = TokenManager(expiry_seconds=7200)
    assert tm.expiry_seconds == 7200


# ---------------------------------------------------------------------------
# auth_fingerprint + credential-change token invalidation
# ---------------------------------------------------------------------------


def test_auth_fingerprint_none_is_stable() -> None:
    assert auth_fingerprint(None) == "open"


def test_auth_fingerprint_single_tuple_is_deterministic() -> None:
    fp1 = auth_fingerprint(("admin", "pass"))
    fp2 = auth_fingerprint(("admin", "pass"))
    assert fp1 == fp2


def test_auth_fingerprint_changes_on_password_change() -> None:
    fp1 = auth_fingerprint(("admin", "old_pass"))
    fp2 = auth_fingerprint(("admin", "new_pass"))
    assert fp1 != fp2


def test_auth_fingerprint_changes_on_username_change() -> None:
    fp1 = auth_fingerprint(("admin", "pass"))
    fp2 = auth_fingerprint(("kwame", "pass"))
    assert fp1 != fp2


def test_auth_fingerprint_list_order_independent() -> None:
    fp1 = auth_fingerprint([("alice", "a1"), ("bob", "b2")])
    fp2 = auth_fingerprint([("bob", "b2"), ("alice", "a1")])
    assert fp1 == fp2


def test_token_invalidated_when_auth_credentials_change() -> None:
    """Tokens issued with old credentials must not verify after auth change.

    This simulates the server restart scenario:
    - App starts with auth=("admin", "pass"), stable secret_key.
    - User logs in; token is stored client-side.
    - App restarts with auth=("kwame", "pass123"), same secret_key.
    - Old token must be rejected by the new TokenManager.
    """
    stable_key = "stable-production-key"

    # Token issued by the original server instance.
    tm_old = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(("admin", "pass")),
    )
    old_token, _ = tm_old.create("admin")

    # New server instance after credentials change.
    tm_new = TokenManager(
        secret_key=stable_key,
        expiry_seconds=3600,
        auth_fingerprint=auth_fingerprint(("kwame", "pass123")),
    )

    # Old token must not verify against the new instance.
    assert tm_new.verify(old_token) is None


def test_token_still_valid_when_auth_unchanged() -> None:
    """Tokens remain valid across restarts when auth config has not changed."""
    stable_key = "stable-production-key"
    fp = auth_fingerprint(("admin", "pass"))

    tm_old = TokenManager(secret_key=stable_key, expiry_seconds=3600, auth_fingerprint=fp)
    token, _ = tm_old.create("admin")

    tm_new = TokenManager(secret_key=stable_key, expiry_seconds=3600, auth_fingerprint=fp)
    result = tm_new.verify(token)
    assert result is not None
    assert result[0] == "admin"
