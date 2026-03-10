"""v0.1 Acceptance criteria test — the exact example from CLAUDE.md.

Verifies end-to-end behaviour:
  GET /payments  → 503 with reason
  GET /debug     → 404 (production env)
  GET /old-endpoint → 503
  GET /health    → 200 always
  /docs does not show /debug or /old-endpoint
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.engine import ShieldEngine
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    disabled,
    env_only,
    force_active,
    maintenance,
)


@pytest.fixture
async def acceptance_app():
    """Build the exact app from the CLAUDE.md acceptance example."""
    engine = ShieldEngine()  # defaults: MemoryBackend, env="production"
    router = ShieldRouter(engine=engine)

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
    app.add_middleware(ShieldMiddleware, engine=engine)
    app.include_router(router)
    apply_shield_to_openapi(app, engine)

    await app.router.startup()
    return app, engine


async def test_payments_returns_503(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/payments")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "MAINTENANCE_MODE"
    assert body["error"]["reason"] == "DB migration"


async def test_debug_returns_404_in_production(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/debug")
    assert resp.status_code == 404
    assert resp.content == b""  # silent


async def test_old_endpoint_returns_503(acceptance_app):
    app, _ = acceptance_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/old-endpoint")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "ROUTE_DISABLED"


async def test_health_always_200(acceptance_app):
    app, engine = acceptance_app
    # Even if we force maintenance on /health, force_active must win.
    await engine.set_maintenance("/health", reason="test")
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
