"""Tests for shield on parameterised routes and prefixed routers.

Covers the two bugs fixed in this session:

1. Parameterised routes — e.g. ``/items/{item_id}`` — were previously
   ignored because ``engine.check()`` received the concrete URL
   (``/items/42``) but the engine stored state under the template key
   (``GET:/items/{item_id}``).  The middleware now resolves the route
   template and uses that for engine lookups.

2. ``ShieldRouter`` with a ``prefix`` — routes were registered without the
   prefix (``GET:/payments``) but the app's route table contained the full
   path (``GET:/api/payments``).  ``ShieldRouter.add_api_route()`` now
   prepends ``self.prefix`` to produce the correct key.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import RouteStatus
from shield.fastapi.decorators import disabled, env_only, force_active, maintenance
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.router import ShieldRouter


def _make_app(env: str = "dev") -> tuple[FastAPI, ShieldEngine]:
    engine = ShieldEngine(backend=MemoryBackend(), current_env=env)
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)
    return app, engine


# ---------------------------------------------------------------------------
# Bug 1: Parameterised routes on plain APIRouter
# ---------------------------------------------------------------------------


async def test_parameterised_route_maintenance_returns_503():
    """/items/{item_id} with @maintenance should block all concrete URLs."""
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/items/{item_id}")
    @maintenance(reason="Rebuilding index")
    async def get_item(item_id: int):
        return {"item_id": item_id}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/items/42")

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "MAINTENANCE_MODE"
    assert body["error"]["reason"] == "Rebuilding index"
    # path in response body shows the *concrete* URL, not the template.
    assert body["error"]["path"] == "/items/42"


async def test_parameterised_route_disabled_returns_503():
    app, _ = _make_app()
    router = APIRouter()

    @router.get("/orders/{order_id}")
    @disabled(reason="Order API retired")
    async def get_order(order_id: str):
        return {"order_id": order_id}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/orders/abc-123")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "ROUTE_DISABLED"


async def test_parameterised_route_env_gated_returns_403():
    app, _ = _make_app(env="production")
    router = APIRouter()

    @router.get("/internal/{resource}")
    @env_only("dev")
    async def internal(resource: str):
        return {"resource": resource}

    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/internal/secrets")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ENV_GATED"


async def test_parameterised_route_force_active_always_passes():
    """`@force_active` on a parameterised route can never be blocked."""
    app, engine = _make_app()
    router = APIRouter()

    @router.get("/health/{service}")
    @force_active
    async def service_health(service: str):
        return {"service": service, "status": "ok"}

    app.include_router(router)

    # Seed maintenance state directly — force_active must override it.
    await engine.backend.set_state(
        "GET:/health/{service}",
        (await engine.backend.get_state("GET:/health/{service}") if False else None)
        or __import__("shield.core.models", fromlist=["RouteState"]).RouteState(
            path="GET:/health/{service}",
            status=RouteStatus.MAINTENANCE,
            reason="Should be bypassed",
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health/auth")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug 2: ShieldRouter with a prefix
# ---------------------------------------------------------------------------


async def test_shield_router_with_prefix_registers_full_path(engine=None):
    """ShieldRouter(prefix='/api') must register 'GET:/api/payments', not
    'GET:/payments'."""
    engine = ShieldEngine(backend=MemoryBackend())
    router = ShieldRouter(engine=engine, prefix="/api")

    @router.get("/payments")
    @maintenance(reason="API migration")
    async def payments():
        return {}

    await router.register_shield_routes()

    # Full path key must exist.
    state = await engine.backend.get_state("GET:/api/payments")
    assert state.status == RouteStatus.MAINTENANCE
    assert state.reason == "API migration"

    # Bare path key must NOT exist.
    with pytest.raises(KeyError):
        await engine.backend.get_state("GET:/payments")


async def test_shield_router_with_prefix_middleware_enforces_state():
    """Middleware must honour the decorator state on a prefixed ShieldRouter."""
    engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = ShieldRouter(engine=engine, prefix="/api")

    @router.get("/invoices")
    @disabled(reason="Billing API offline")
    async def invoices():
        return {}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/invoices")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "ROUTE_DISABLED"


async def test_shield_router_with_prefix_active_route_passes():
    """Undecorated routes on a prefixed ShieldRouter still pass through."""
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = ShieldRouter(engine=engine, prefix="/v2")

    @router.get("/users")
    async def users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v2/users")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Combined: parameterised route on prefixed ShieldRouter
# ---------------------------------------------------------------------------


async def test_prefixed_shield_router_parameterised_route():
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = ShieldRouter(engine=engine, prefix="/api")

    @router.get("/items/{item_id}")
    @maintenance(reason="Index rebuild")
    async def get_item(item_id: int):
        return {"item_id": item_id}

    app.include_router(router)
    await app.router.startup()

    # Both the registered key and the middleware lookup must use the full
    # template: GET:/api/items/{item_id}
    state = await engine.backend.get_state("GET:/api/items/{item_id}")
    assert state.status == RouteStatus.MAINTENANCE

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/items/7")

    assert resp.status_code == 503
    assert resp.json()["error"]["path"] == "/api/items/7"
