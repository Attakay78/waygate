"""Integration tests: shield decorators on plain APIRouter (not ShieldRouter).

These tests verify that ``@maintenance``, ``@disabled``, ``@env_only``, and
``@deprecated`` work correctly when applied to routes on a vanilla
``fastapi.APIRouter`` or directly on the ``FastAPI`` app — without
``ShieldRouter`` being involved.

The ``ShieldMiddleware`` lazy-scans all app routes on the first request and
registers any ``__shield_meta__``-bearing endpoints with the engine.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.fastapi.decorators import deprecated, disabled, env_only, maintenance
from shield.fastapi.middleware import ShieldMiddleware


def _make_app(env: str = "production") -> tuple[FastAPI, ShieldEngine]:
    engine = ShieldEngine(backend=MemoryBackend(), current_env=env)
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)
    return app, engine


# ---------------------------------------------------------------------------
# @maintenance on plain APIRouter
# ---------------------------------------------------------------------------


async def test_plain_router_maintenance_returns_503():
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/orders")
    @maintenance(reason="Upgrading DB")
    async def get_orders():
        return {"orders": []}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/orders")

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "MAINTENANCE_MODE"
    assert body["error"]["reason"] == "Upgrading DB"


# ---------------------------------------------------------------------------
# @disabled on plain APIRouter
# ---------------------------------------------------------------------------


async def test_plain_router_disabled_returns_503():
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/legacy")
    @disabled(reason="Use /v2/legacy")
    async def legacy():
        return {}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/legacy")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "ROUTE_DISABLED"


# ---------------------------------------------------------------------------
# @env_only on plain APIRouter
# ---------------------------------------------------------------------------


async def test_plain_router_env_gated_returns_404_in_production():
    app, _ = _make_app(env="production")
    router = APIRouter()

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")

    assert resp.status_code == 404


async def test_plain_router_env_gated_passes_in_allowed_env():
    app, _ = _make_app(env="dev")
    router = APIRouter()

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# @deprecated on plain APIRouter — requests pass but headers injected
# ---------------------------------------------------------------------------


async def test_plain_router_deprecated_injects_headers():
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/v1/items")
    @deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/items")
    async def get_items_v1():
        return {"items": []}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/items")

    assert resp.status_code == 200
    assert resp.headers["deprecation"] == "true"
    assert "Sat, 01 Jan 2027" in resp.headers["sunset"]
    assert "/v2/items" in resp.headers["link"]


# ---------------------------------------------------------------------------
# Routes directly on the FastAPI app (no router)
# ---------------------------------------------------------------------------


async def test_app_level_route_maintenance_returns_503():
    app, engine = _make_app()

    @app.get("/status")
    @maintenance(reason="Planned downtime")
    async def status():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")

    assert resp.status_code == 503
    assert resp.json()["error"]["reason"] == "Planned downtime"


# ---------------------------------------------------------------------------
# Undecorated plain APIRouter route still passes through
# ---------------------------------------------------------------------------


async def test_plain_router_undecorated_passes_through():
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# scan_routes() standalone function
# ---------------------------------------------------------------------------


async def test_scan_routes_standalone():
    """scan_routes() can be called manually (e.g. in a lifespan) to register
    all decorated routes before the first request."""
    from shield.fastapi.router import scan_routes

    engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
    app = FastAPI()
    router = APIRouter()

    @router.get("/invoices")
    @disabled(reason="Migrating")
    async def invoices():
        return {}

    app.include_router(router)

    # Call explicitly — simulates lifespan usage.
    await scan_routes(app, engine)

    state = await engine.backend.get_state("GET:/invoices")
    assert state.status.value == "disabled"
