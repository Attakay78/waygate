"""Integration tests for WaygateMiddleware.

Each test spins up a minimal FastAPI app and makes real HTTP requests via
``httpx.AsyncClient`` + ``ASGITransport``.  No real server is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fastapi._helpers import _trigger_startup
from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.core.models import MaintenanceWindow
from waygate.fastapi.decorators import disabled, env_only, force_active, maintenance
from waygate.fastapi.middleware import WaygateMiddleware
from waygate.fastapi.router import WaygateRouter


def _build_app(env: str = "dev") -> tuple[FastAPI, WaygateEngine]:
    """Return a bare (app, engine) pair — routes added by the caller."""
    engine = WaygateEngine(backend=MemoryBackend(), current_env=env)
    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    return app, engine


def _include(app: FastAPI, router: WaygateRouter) -> None:
    """Include router into app — must be called AFTER routes are defined."""
    app.include_router(router)


async def _startup(app: FastAPI) -> None:
    await _trigger_startup(app)


# ---------------------------------------------------------------------------
# Maintenance mode → 503
# ---------------------------------------------------------------------------


async def test_maintenance_returns_503():
    app, engine = _build_app()
    router = WaygateRouter(engine=engine)

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments():
        return {"ok": True}

    _include(app, router)
    await _startup(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/payments")

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "MAINTENANCE_MODE"
    assert body["error"]["reason"] == "DB migration"
    assert body["error"]["path"] == "/payments"


async def test_maintenance_sets_retry_after_header():
    app, engine = _build_app()
    await engine.register("/api/pay", {"status": "active"})
    window = MaintenanceWindow(
        start=datetime(2025, 3, 10, 2, 0, tzinfo=UTC),
        end=datetime(2025, 3, 10, 4, 0, tzinfo=UTC),
    )
    await engine.set_maintenance("/api/pay", reason="test", window=window)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/pay")

    assert resp.status_code == 503
    assert "Retry-After" in resp.headers
    assert "2025-03-10T04:00:00" in resp.headers["Retry-After"]


# ---------------------------------------------------------------------------
# Disabled route → 503
# ---------------------------------------------------------------------------


async def test_disabled_returns_503():
    app, engine = _build_app()
    router = WaygateRouter(engine=engine)

    @router.get("/old-endpoint")
    @disabled(reason="Use /new-endpoint instead")
    async def old_endpoint():
        return {"ok": True}

    _include(app, router)
    await _startup(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/old-endpoint")

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "ROUTE_DISABLED"
    assert "new-endpoint" in body["error"]["reason"]


# ---------------------------------------------------------------------------
# ENV_GATED route
# ---------------------------------------------------------------------------


async def test_env_gated_wrong_env_returns_403():
    app, engine = _build_app(env="production")
    router = WaygateRouter(engine=engine)

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    _include(app, router)
    await _startup(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "ENV_GATED"
    assert body["error"]["current_env"] == "production"
    assert body["error"]["allowed_envs"] == ["dev"]


async def test_env_gated_correct_env_passes():
    app, engine = _build_app(env="dev")
    router = WaygateRouter(engine=engine)

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    _include(app, router)
    await _startup(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# force_active → always 200
# ---------------------------------------------------------------------------


async def test_force_active_bypasses_engine():
    app, engine = _build_app()
    router = WaygateRouter(engine=engine)

    @router.get("/health")
    @force_active
    async def health():
        return {"status": "ok"}

    _include(app, router)
    await _startup(app)

    # @force_active routes are registered as "GET:/health" with force_active=True.
    # Trying to mutate that key directly raises RouteProtectedException.
    from waygate.core.exceptions import RouteProtectedException

    with pytest.raises(RouteProtectedException):
        await engine.disable("GET:/health", reason="oops")

    # A path-level state ("/health") does not trigger the guard because no
    # such key exists in the backend — the middleware bypass catches it instead.
    # Either way the response must be 200.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Active (undecorated) route → 200
# ---------------------------------------------------------------------------


async def test_active_route_passes():
    app, engine = _build_app()
    router = WaygateRouter(engine=engine)

    @router.get("/api/users")
    async def users():
        return {"users": []}

    _include(app, router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/users")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Docs/OpenAPI paths are always skipped
# ---------------------------------------------------------------------------


async def test_docs_path_is_skipped():
    app, engine = _build_app()
    await engine.register("/docs", {"status": "active"})
    await engine.set_maintenance("/docs", reason="test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/docs")

    assert resp.status_code == 200


async def test_openapi_json_is_skipped():
    app, engine = _build_app()
    await engine.register("/openapi.json", {"status": "active"})
    await engine.set_maintenance("/openapi.json", reason="test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/openapi.json")

    assert resp.status_code == 200
