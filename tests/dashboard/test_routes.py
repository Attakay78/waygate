"""Tests for the Shield dashboard route handlers (v0.3)."""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from shield.core.engine import ShieldEngine
from shield.core.models import MaintenanceWindow, RouteState, RouteStatus
from shield.dashboard.app import ShieldDashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_path(path: str) -> str:
    """Base64url-encode *path* for use in a URL segment (mirrors routes.py)."""
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> ShieldEngine:
    """Provide a ShieldEngine pre-loaded with a couple of test routes."""
    e = ShieldEngine()
    await e.backend.set_state("/payments", RouteState(path="/payments", status=RouteStatus.ACTIVE))
    await e.backend.set_state("/health", RouteState(path="/health", status=RouteStatus.ACTIVE))
    return e


@pytest.fixture
def dashboard(engine: ShieldEngine) -> object:
    """Return a ShieldDashboard ASGI app (no auth)."""
    return ShieldDashboard(engine=engine)


@pytest.fixture
async def client(dashboard: object) -> AsyncClient:
    """Return an httpx AsyncClient pointing at the dashboard."""
    async with AsyncClient(
        transport=ASGITransport(app=dashboard),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Index / routes page
# ---------------------------------------------------------------------------


async def test_index_returns_200(client: AsyncClient) -> None:
    """GET / renders the routes page with status 200."""
    resp = await client.get("/")
    assert resp.status_code == 200


async def test_index_contains_shield_brand(client: AsyncClient) -> None:
    """Index page contains the Shield brand name."""
    resp = await client.get("/")
    assert "Shield" in resp.text


async def test_index_shows_registered_routes(client: AsyncClient) -> None:
    """Index page lists registered route paths."""
    resp = await client.get("/")
    assert "/payments" in resp.text
    assert "/health" in resp.text


async def test_index_shows_status_badge(client: AsyncClient) -> None:
    """Index page contains a status badge."""
    resp = await client.get("/")
    assert "active" in resp.text.lower()


# ---------------------------------------------------------------------------
# Routes partial
# ---------------------------------------------------------------------------


async def test_routes_partial_returns_200(client: AsyncClient) -> None:
    """GET /routes returns 200 with table rows partial."""
    resp = await client.get("/routes")
    assert resp.status_code == 200
    assert "/payments" in resp.text


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


async def test_toggle_active_to_maintenance(client: AsyncClient, engine: ShieldEngine) -> None:
    """POST /toggle/{key} puts an active route into maintenance."""
    resp = await client.post(f"/toggle/{_encode_path('/payments')}")
    assert resp.status_code == 200
    # Response is an HTML partial containing the updated row.
    assert "row-payments" in resp.text
    assert "maintenance" in resp.text.lower()

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.MAINTENANCE


async def test_toggle_maintenance_to_active(client: AsyncClient, engine: ShieldEngine) -> None:
    """POST /toggle/{key} re-enables a route that is in maintenance."""
    await engine.set_maintenance("/payments", reason="test")
    resp = await client.post(f"/toggle/{_encode_path('/payments')}")
    assert resp.status_code == 200
    assert "active" in resp.text.lower()

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.ACTIVE


# ---------------------------------------------------------------------------
# Disable
# ---------------------------------------------------------------------------


async def test_disable_route(client: AsyncClient, engine: ShieldEngine) -> None:
    """POST /disable/{key} disables a route and returns the updated partial."""
    resp = await client.post(f"/disable/{_encode_path('/payments')}")
    assert resp.status_code == 200
    assert "disabled" in resp.text.lower()

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.DISABLED


# ---------------------------------------------------------------------------
# Enable
# ---------------------------------------------------------------------------


async def test_enable_route(client: AsyncClient, engine: ShieldEngine) -> None:
    """POST /enable/{key} re-enables a disabled route."""
    await engine.disable("/payments", reason="test")
    resp = await client.post(f"/enable/{_encode_path('/payments')}")
    assert resp.status_code == 200
    assert "active" in resp.text.lower()

    state = await engine.get_state("/payments")
    assert state.status == RouteStatus.ACTIVE


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


async def test_schedule_maintenance_window(client: AsyncClient, engine: ShieldEngine) -> None:
    """POST /schedule sets a future maintenance window via form data."""
    start = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    end = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")

    resp = await client.post(
        "/schedule",
        data={
            "path": "/payments",
            "start": start,
            "end": end,
            "reason": "Scheduled migration",
        },
    )
    assert resp.status_code == 200
    assert "row-payments" in resp.text


# ---------------------------------------------------------------------------
# Cancel schedule
# ---------------------------------------------------------------------------


async def test_cancel_schedule(client: AsyncClient, engine: ShieldEngine) -> None:
    """DELETE /schedule/{key} cancels a pending scheduled window."""
    window = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
        reason="Test window",
    )
    await engine.schedule_maintenance("/payments", window, actor="test")

    resp = await client.delete(f"/schedule/{_encode_path('/payments')}")
    assert resp.status_code == 200
    assert "row-payments" in resp.text


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def test_audit_page_returns_200(client: AsyncClient, engine: ShieldEngine) -> None:
    """GET /audit renders the audit log page."""
    await engine.disable("/payments", reason="audit-test", actor="tester")
    resp = await client.get("/audit")
    assert resp.status_code == 200


async def test_audit_page_contains_entries(client: AsyncClient, engine: ShieldEngine) -> None:
    """Audit page lists recent state changes."""
    await engine.disable("/payments", reason="for-audit", actor="tester")
    resp = await client.get("/audit")
    assert "disable" in resp.text


async def test_audit_rows_partial(client: AsyncClient, engine: ShieldEngine) -> None:
    """GET /audit/rows returns only the rows partial (for HTMX auto-refresh)."""
    await engine.enable("/payments", actor="auto-refresh-test")
    resp = await client.get("/audit/rows")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def test_sse_handler_returns_streaming_response(engine: ShieldEngine) -> None:
    """events() handler returns a StreamingResponse with text/event-stream media type."""

    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from shield.dashboard import routes as r

    app = ShieldDashboard(engine=engine)
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    request = Request(scope)
    response = await r.events(request)

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    assert "no-cache" in response.headers.get("cache-control", "")


async def test_sse_generator_emits_on_state_change(engine: ShieldEngine) -> None:
    """SSE generator yields ``shield:update:*`` event when a route state changes."""
    import asyncio

    from starlette.requests import Request

    from shield.dashboard import routes as r

    app = ShieldDashboard(engine=engine)
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    request = Request(scope)
    response = await r.events(request)

    # Trigger a state change slightly after the generator starts listening.
    async def _trigger() -> None:
        await asyncio.sleep(0.02)
        await engine.disable("/payments", reason="sse-test", actor="test")

    trigger_task = asyncio.create_task(_trigger())
    gen = response.body_iterator  # type: ignore[union-attr]
    first_chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)  # type: ignore[union-attr]
    trigger_task.cancel()
    try:
        await trigger_task
    except asyncio.CancelledError:
        pass

    text = first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
    assert "shield:update:" in text


async def test_sse_keepalive_when_subscribe_unsupported() -> None:
    """SSE generator sends keepalive comment when backend raises NotImplementedError."""
    import asyncio
    import unittest.mock

    from starlette.requests import Request

    from shield.dashboard import routes as r

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        f.write('{"states": {}, "audit": [], "global": null}')
        fname = f.name

    try:
        from shield.core.backends.file import FileBackend

        file_engine = ShieldEngine(backend=FileBackend(fname))
        app = ShieldDashboard(engine=file_engine)
        scope: dict = {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "query_string": b"",
            "headers": [],
            "app": app,
        }
        request = Request(scope)
        response = await r.events(request)

        # Patch anyio.sleep to return immediately so the keepalive fires instantly.
        with unittest.mock.patch("shield.dashboard.routes.anyio.sleep", return_value=None):
            gen = response.body_iterator  # type: ignore[union-attr]
            first_chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)  # type: ignore[union-attr]

        text = first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
        assert ": keepalive" in text
    finally:
        os.unlink(fname)


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------


async def test_basic_auth_blocks_unauthenticated() -> None:
    """Dashboard with auth configured returns 401 for unauthenticated requests."""
    e = ShieldEngine()
    app = ShieldDashboard(engine=e, auth=("admin", "s3cr3t"))

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers
    assert 'Basic realm="Shield Dashboard"' in resp.headers["WWW-Authenticate"]


async def test_basic_auth_allows_valid_credentials() -> None:
    """Dashboard with auth passes requests that carry correct credentials."""
    e = ShieldEngine()
    app = ShieldDashboard(engine=e, auth=("admin", "s3cr3t"))

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
        auth=("admin", "s3cr3t"),
    ) as c:
        resp = await c.get("/")
    assert resp.status_code == 200


async def test_basic_auth_rejects_wrong_password() -> None:
    """Dashboard with auth rejects requests with an incorrect password."""
    e = ShieldEngine()
    app = ShieldDashboard(engine=e, auth=("admin", "s3cr3t"))

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
        auth=("admin", "wrong"),
    ) as c:
        resp = await c.get("/")
    assert resp.status_code == 401


async def test_basic_auth_rejects_missing_header() -> None:
    """Dashboard returns 401 when Authorization header is absent."""
    e = ShieldEngine()
    app = ShieldDashboard(engine=e, auth=("admin", "s3cr3t"))

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/audit")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OpenAPI isolation
# ---------------------------------------------------------------------------


async def test_dashboard_does_not_pollute_parent_openapi() -> None:
    """Mounting the dashboard does not add routes to the parent FastAPI schema."""
    from fastapi import FastAPI

    e = ShieldEngine()
    fastapi_app = FastAPI()
    fastapi_app.mount("/shield", ShieldDashboard(engine=e))

    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/openapi.json")
    schema = resp.json()
    paths = schema.get("paths", {})
    assert not any(p.startswith("/shield") for p in paths), (
        f"Dashboard paths leaked into OpenAPI schema: "
        f"{[p for p in paths if p.startswith('/shield')]}"
    )


async def test_dashboard_accessible_when_mounted_on_fastapi() -> None:
    """Dashboard routes are reachable when mounted on a FastAPI parent app."""
    from fastapi import FastAPI

    e = ShieldEngine()
    fastapi_app = FastAPI()
    fastapi_app.mount("/shield", ShieldDashboard(engine=e))

    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/shield/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Method-prefixed route keys
# ---------------------------------------------------------------------------


async def test_toggle_method_prefixed_route(engine: ShieldEngine) -> None:
    """Dashboard handles method-prefixed route keys like ``GET:/payments``."""
    key = "GET:/payments"
    await engine.backend.set_state(key, RouteState(path=key, status=RouteStatus.ACTIVE))

    app = ShieldDashboard(engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        resp = await c.post(f"/toggle/{_encode_path(key)}")
    assert resp.status_code == 200
