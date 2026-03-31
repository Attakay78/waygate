"""v0.1 Acceptance criteria test — the exact example from CLAUDE.md.

Verifies end-to-end behaviour:
  GET /payments  → 503 with reason
  GET /debug     → 403 (production env)
  GET /old-endpoint → 503
  GET /health    → 200 always
  /docs does not show /debug or /old-endpoint
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fastapi._helpers import _trigger_startup
from waygate.core.engine import WaygateEngine
from waygate.fastapi import (
    WaygateMiddleware,
    WaygateRouter,
    apply_waygate_to_openapi,
    disabled,
    env_only,
    force_active,
    maintenance,
)


@pytest.fixture
async def acceptance_app():
    """Build the exact app from the CLAUDE.md acceptance example."""
    engine = WaygateEngine(
        current_env="production"
    )  # explicit: acceptance scenario tests env-gating in production
    router = WaygateRouter(engine=engine)

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments():
        return {"ok": True}

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    @router.get("/old-endpoint")
    @disabled(reason="Use /new-endpoint instead")
    async def old_endpoint():
        return {"ok": True}

    @router.get("/health")
    @force_active
    async def health():
        return {"status": "ok"}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)
    apply_waygate_to_openapi(app, engine)

    await _trigger_startup(app)
    return app, engine


async def test_payments_returns_503(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/payments")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "MAINTENANCE_MODE"
    assert body["error"]["reason"] == "DB migration"


async def test_debug_returns_403_in_production(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "ENV_GATED"


async def test_old_endpoint_returns_503(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/old-endpoint")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "ROUTE_DISABLED"


async def test_health_always_200(acceptance_app):
    app, engine = acceptance_app
    # force_active routes are immune to state changes — the engine rejects them.
    from waygate.core.exceptions import RouteProtectedException

    with pytest.raises(RouteProtectedException):
        await engine.set_maintenance("/health", reason="test")
    # health endpoint is always reachable
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_docs_hides_debug_and_old_endpoint(acceptance_app):
    app, _ = acceptance_app
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/debug" not in paths, "/debug must be hidden from docs (env-gated)"
    assert "/old-endpoint" not in paths, "/old-endpoint must be hidden (disabled)"


async def test_docs_shows_payments_and_health(acceptance_app):
    app, _ = acceptance_app
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/payments" in paths
    assert "/health" in paths
