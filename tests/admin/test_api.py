"""Tests for the ShieldAdmin REST API (CLI back-end).

All tests use an in-process ASGI transport so no real server is needed.
Auth is tested both with and without credentials configured.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from shield.admin.app import ShieldAdmin
from shield.core.engine import ShieldEngine
from shield.core.models import RouteState, RouteStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> ShieldEngine:
    """Engine pre-seeded with two routes."""
    e = ShieldEngine()
    await e.backend.set_state("/payments", RouteState(path="/payments", status=RouteStatus.ACTIVE))
    await e.backend.set_state("/health", RouteState(path="/health", status=RouteStatus.ACTIVE))
    return e


@pytest.fixture
def admin_no_auth(engine: ShieldEngine) -> object:
    """ShieldAdmin without auth — open access."""
    return ShieldAdmin(engine=engine)


@pytest.fixture
def admin_with_auth(engine: ShieldEngine) -> object:
    """ShieldAdmin with single-user auth."""
    return ShieldAdmin(engine=engine, auth=("admin", "secret"))


@pytest.fixture
async def open_client(admin_no_auth: object) -> AsyncClient:
    """httpx client for the open-access admin app."""
    async with AsyncClient(
        transport=ASGITransport(app=admin_no_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
async def auth_client(admin_with_auth: object) -> AsyncClient:
    """httpx client that has already logged in."""
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        # Log in and inject token for subsequent requests.
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        assert resp.status_code == 200
        token = resp.json()["token"]
        c.headers.update({"X-Shield-Token": token})
        yield c


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


async def test_login_success(admin_with_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["username"] == "admin"
    assert "expires_at" in body


async def test_login_wrong_password(admin_with_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


async def test_login_missing_fields(admin_with_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        resp = await c.post("/api/auth/login", json={"username": "admin"})
    assert resp.status_code == 400


async def test_login_no_auth_configured_returns_501(admin_no_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_no_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        resp = await c.post("/api/auth/login", json={"username": "u", "password": "p"})
    assert resp.status_code == 501


async def test_auth_me_returns_actor(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "admin"
    assert body["platform"] == "cli"


async def test_auth_me_no_auth(open_client: AsyncClient) -> None:
    resp = await open_client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "anonymous"


async def test_logout_revokes_token(admin_with_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        # Login.
        resp = await c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        token = resp.json()["token"]
        c.headers.update({"X-Shield-Token": token})

        # Verify it works.
        resp = await c.get("/api/auth/me")
        assert resp.status_code == 200

        # Logout.
        resp = await c.post("/api/auth/logout")
        assert resp.status_code == 200

        # Token should now be rejected.
        resp = await c.get("/api/routes")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected routes require auth
# ---------------------------------------------------------------------------


async def test_api_routes_without_token_returns_401(admin_with_auth: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=admin_with_auth),  # type: ignore[arg-type]
        base_url="http://testserver",
        headers={"X-Shield-Platform": "cli"},
    ) as c:
        resp = await c.get("/api/routes")
    assert resp.status_code == 401


async def test_api_routes_with_valid_token_returns_200(auth_client: AsyncClient) -> None:
    resp = await auth_client.get("/api/routes")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Route CRUD
# ---------------------------------------------------------------------------


async def test_list_routes(open_client: AsyncClient) -> None:
    resp = await open_client.get("/api/routes")
    assert resp.status_code == 200
    paths = [r["path"] for r in resp.json()]
    assert "/payments" in paths
    assert "/health" in paths


async def test_get_route(open_client: AsyncClient) -> None:
    import base64

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    resp = await open_client.get(f"/api/routes/{key}")
    assert resp.status_code == 200
    assert resp.json()["path"] == "/payments"


async def test_disable_route(open_client: AsyncClient, engine: ShieldEngine) -> None:
    import base64

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    resp = await open_client.post(f"/api/routes/{key}/disable", json={"reason": "test-disable"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.DISABLED


async def test_enable_route(open_client: AsyncClient, engine: ShieldEngine) -> None:
    import base64

    await engine.disable("/payments", reason="setup")
    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    resp = await open_client.post(f"/api/routes/{key}/enable", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_maintenance_route(open_client: AsyncClient, engine: ShieldEngine) -> None:
    import base64

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    resp = await open_client.post(f"/api/routes/{key}/maintenance", json={"reason": "DB migration"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "maintenance"

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.MAINTENANCE


async def test_maintenance_route_with_window(
    open_client: AsyncClient, engine: ShieldEngine
) -> None:
    import base64
    from datetime import UTC, datetime, timedelta

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    start = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    end = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = await open_client.post(
        f"/api/routes/{key}/maintenance",
        json={"reason": "scheduled", "start": start, "end": end},
    )
    assert resp.status_code == 200


async def test_schedule_route(open_client: AsyncClient) -> None:
    import base64
    from datetime import UTC, datetime, timedelta

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    start = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    end = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    resp = await open_client.post(
        f"/api/routes/{key}/schedule",
        json={"start": start, "end": end, "reason": "planned work"},
    )
    assert resp.status_code == 200


async def test_cancel_schedule_route(open_client: AsyncClient, engine: ShieldEngine) -> None:
    import base64
    from datetime import UTC, datetime, timedelta

    from shield.core.models import MaintenanceWindow

    window = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
        reason="to cancel",
    )
    await engine.schedule_maintenance("/payments", window, actor="test")

    key = base64.urlsafe_b64encode(b"/payments").decode().rstrip("=")
    resp = await open_client.delete(f"/api/routes/{key}/schedule")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_list_audit(open_client: AsyncClient, engine: ShieldEngine) -> None:
    await engine.disable("/payments", reason="audit-test", actor="tester")
    resp = await open_client.get("/api/audit")
    assert resp.status_code == 200
    entries = resp.json()
    assert isinstance(entries, list)
    assert len(entries) > 0


async def test_list_audit_filter_by_route(open_client: AsyncClient, engine: ShieldEngine) -> None:
    await engine.disable("/payments", reason="audit-route-filter", actor="tester")
    resp = await open_client.get("/api/audit?route=/payments&limit=5")
    assert resp.status_code == 200
    entries = resp.json()
    assert all(e["path"] == "/payments" for e in entries)


# ---------------------------------------------------------------------------
# Global maintenance
# ---------------------------------------------------------------------------


async def test_get_global(open_client: AsyncClient) -> None:
    resp = await open_client.get("/api/global")
    assert resp.status_code == 200
    assert "enabled" in resp.json()


async def test_global_enable_disable_round_trip(
    open_client: AsyncClient, engine: ShieldEngine
) -> None:
    resp = await open_client.post(
        "/api/global/enable", json={"reason": "all down", "exempt_paths": ["/health"]}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    resp = await open_client.post("/api/global/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ---------------------------------------------------------------------------
# Actor is recorded correctly
# ---------------------------------------------------------------------------


async def test_actor_recorded_in_audit_no_auth(
    open_client: AsyncClient, engine: ShieldEngine
) -> None:
    """With no auth, actor header is used."""
    import base64

    key = base64.urlsafe_b64encode(b"/health").decode().rstrip("=")
    resp = await open_client.post(
        f"/api/routes/{key}/disable",
        json={"reason": "actor-test"},
        headers={"X-Shield-Actor": "ops-bot"},
    )
    assert resp.status_code == 200
    entries = await engine.get_audit_log(path="/health", limit=1)
    assert entries[0].actor == "ops-bot"


async def test_actor_recorded_in_audit_with_auth(
    auth_client: AsyncClient, engine: ShieldEngine
) -> None:
    """With auth, actor is the authenticated username."""
    import base64

    key = base64.urlsafe_b64encode(b"/health").decode().rstrip("=")
    await auth_client.post(f"/api/routes/{key}/disable", json={"reason": "auth-actor-test"})
    entries = await engine.get_audit_log(path="/health", limit=1)
    assert entries[0].actor == "admin"
