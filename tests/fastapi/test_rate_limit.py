"""Integration tests for @rate_limit decorator + middleware + engine."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from waygate.core.engine import WaygateEngine
from waygate.core.rate_limit.storage import HAS_LIMITS
from waygate.fastapi import WaygateMiddleware, WaygateRouter
from waygate.fastapi.decorators import rate_limit

pytestmark = pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def _make_app(limit_str: str, key: str = "ip") -> tuple[FastAPI, WaygateEngine]:
    """Create a minimal FastAPI app with a rate-limited route."""
    engine = WaygateEngine()
    router = WaygateRouter(engine=engine)

    @router.get("/items")
    @rate_limit(limit_str, key=key)
    async def list_items():
        return {"items": []}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)
    return app, engine


# ---------------------------------------------------------------------------
# Basic enforcement
# ---------------------------------------------------------------------------


async def test_requests_within_limit_pass():
    app, _ = _make_app("5/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(5):
            resp = await c.get("/items")
            assert resp.status_code == 200


async def test_request_exceeding_limit_returns_429():
    app, _ = _make_app("3/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(3):
            await c.get("/items")
        resp = await c.get("/items")
    assert resp.status_code == 429


async def test_429_body_has_error_structure():
    app, _ = _make_app("1/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/items")
        resp = await c.get("/items")
    assert resp.status_code == 429
    body = resp.json()
    assert "error" in body
    error = body["error"]
    assert error["code"] == "RATE_LIMIT_EXCEEDED"
    assert "limit" in error


async def test_429_has_retry_after_header():
    app, _ = _make_app("1/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/items")
        resp = await c.get("/items")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_allowed_responses_have_ratelimit_headers():
    app, _ = _make_app("10/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/items")
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers
    assert "X-RateLimit-Reset" in resp.headers


async def test_x_ratelimit_remaining_decrements():
    app, _ = _make_app("10/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.get("/items")
        r2 = await c.get("/items")
    remaining_1 = int(r1.headers["X-RateLimit-Remaining"])
    remaining_2 = int(r2.headers["X-RateLimit-Remaining"])
    assert remaining_2 < remaining_1


# ---------------------------------------------------------------------------
# Limit string formats
# ---------------------------------------------------------------------------


async def test_per_second_limit():
    app, _ = _make_app("2/second")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(2):
            resp = await c.get("/items")
            assert resp.status_code == 200
        resp = await c.get("/items")
    assert resp.status_code == 429


async def test_per_hour_limit_allows():
    app, _ = _make_app("1000/hour")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(10):
            resp = await c.get("/items")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Key strategy: GLOBAL
# ---------------------------------------------------------------------------


async def test_global_key_strategy_shares_counter():
    """With key='global' all callers share the same counter."""
    app, _ = _make_app("2/minute", key="global")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Simulate two different callers (different X-Forwarded-For) consuming the limit
        await c.get("/items", headers={"X-Forwarded-For": "1.2.3.4"})
        await c.get("/items", headers={"X-Forwarded-For": "5.6.7.8"})
        # Third request from a different IP — still blocked because counter is shared
        resp = await c.get("/items", headers={"X-Forwarded-For": "9.10.11.12"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Policy registration
# ---------------------------------------------------------------------------


async def test_rate_limit_policy_is_registered_on_engine():
    app, engine = _make_app("10/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Making a request triggers the ASGI startup event (WaygateRouter.register_waygate_routes)
        await c.get("/items")
    assert len(engine._rate_limit_policies) >= 1


# ---------------------------------------------------------------------------
# Engine.get_rate_limit_hits
# ---------------------------------------------------------------------------


async def test_blocked_requests_are_recorded():
    app, engine = _make_app("1/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/items")  # allowed
        await c.get("/items")  # blocked — hit recorded

    hits = await engine.get_rate_limit_hits()
    assert len(hits) == 1
    assert hits[0].path == "/items"
    assert hits[0].method == "GET"


async def test_allowed_requests_are_not_recorded_as_hits():
    app, engine = _make_app("100/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(5):
            await c.get("/items")

    hits = await engine.get_rate_limit_hits()
    assert len(hits) == 0


# ---------------------------------------------------------------------------
# Routes not decorated with @rate_limit are unaffected
# ---------------------------------------------------------------------------


async def test_undecorated_route_is_never_rate_limited():
    engine = WaygateEngine()
    router = WaygateRouter(engine=engine)

    @router.get("/free")
    async def free_route():
        return {"ok": True}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(20):
            resp = await c.get("/free")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Maintenance mode short-circuits before rate limit check
# ---------------------------------------------------------------------------


async def test_maintenance_mode_does_not_consume_quota():
    engine = WaygateEngine()
    router = WaygateRouter(engine=engine)

    @router.get("/maint-route")
    @rate_limit("2/minute")
    async def maint_route():
        return {"ok": True}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # First request triggers startup (route registration)
        await c.get("/maint-route")
        # Put the route in maintenance
        await engine.set_maintenance("/maint-route", reason="test")

        # Send many requests — should all get 503, not 429
        for _ in range(10):
            resp = await c.get("/maint-route")
            assert resp.status_code == 503

    # Restore and check limit is not consumed
    await engine.enable("/maint-route")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/maint-route")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# engine.reset_rate_limit
# ---------------------------------------------------------------------------


async def test_reset_rate_limit_clears_counters():
    app, engine = _make_app("2/minute")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(2):
            await c.get("/items")
        # Now at limit
        resp = await c.get("/items")
        assert resp.status_code == 429

    # Reset counters
    await engine.reset_rate_limit(path="/items")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/items")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# @rate_limit with algorithm kwarg
# ---------------------------------------------------------------------------


async def test_fixed_window_algorithm():
    engine = WaygateEngine()
    router = WaygateRouter(engine=engine)

    @router.get("/fw")
    @rate_limit("3/minute", algorithm="fixed_window")
    async def fw_route():
        return {"ok": True}

    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(3):
            resp = await c.get("/fw")
            assert resp.status_code == 200
        resp = await c.get("/fw")
    assert resp.status_code == 429
