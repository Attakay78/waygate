"""Integration tests for bypass_rate_limits flag and waygate.testing.bypass."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from waygate.core.engine import WaygateEngine
from waygate.core.rate_limit.storage import HAS_LIMITS
from waygate.fastapi import WaygateMiddleware, WaygateRouter
from waygate.fastapi.decorators import rate_limit
from waygate.testing import bypass

pytestmark = pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


def _make_limited_app(
    limit_str: str,
    bypass_rate_limits: bool = False,
) -> tuple[FastAPI, WaygateEngine]:
    engine = WaygateEngine(bypass_rate_limits=bypass_rate_limits)
    router = WaygateRouter(engine=engine)

    @router.get("/items")
    @rate_limit(limit_str)
    async def list_items():
        return {"items": []}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)
    return app, engine


# ---------------------------------------------------------------------------
# bypass_rate_limits constructor flag
# ---------------------------------------------------------------------------


async def test_bypass_rate_limits_allows_requests_over_limit():
    """bypass_rate_limits=True lets all requests through regardless of quota."""
    app, _ = _make_limited_app("2/minute", bypass_rate_limits=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(5):
            resp = await c.get("/items")
            assert resp.status_code == 200


async def test_bypass_rate_limits_false_still_enforces():
    """Default engine (bypass_rate_limits=False) still returns 429 over limit."""
    app, _ = _make_limited_app("2/minute", bypass_rate_limits=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(2):
            await c.get("/items")
        resp = await c.get("/items")
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# waygate.testing.bypass context manager
# ---------------------------------------------------------------------------


async def test_bypass_context_manager_skips_rate_limits():
    """bypass(rate_limits=True) inside a block lets over-limit requests through."""
    app, engine = _make_limited_app("2/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # exhaust the limit
        for _ in range(2):
            await c.get("/items")

        # 429 without bypass
        resp = await c.get("/items")
        assert resp.status_code == 429

        # reset counters so bypass can be tested cleanly
        await engine.reset_rate_limit("/items", "GET")

        # bypass active: over-limit request passes
        with bypass(engine, rate_limits=True, lifecycle=False):
            for _ in range(5):
                resp = await c.get("/items")
                assert resp.status_code == 200


async def test_bypass_context_manager_restores_rate_limit_enforcement():
    """Rate limits are re-enforced after the bypass block exits."""
    app, engine = _make_limited_app("2/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        with bypass(engine, rate_limits=True, lifecycle=False):
            for _ in range(5):
                await c.get("/items")

        # bypass exited — limit should now be enforced again
        # reset counters first so we start fresh
        await engine.reset_rate_limit("/items", "GET")
        for _ in range(2):
            await c.get("/items")
        resp = await c.get("/items")
        assert resp.status_code == 429
