"""Tests for @deprecated decorator and deprecation header injection."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import RouteStatus
from shield.fastapi.decorators import deprecated
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.router import ShieldRouter


def _build_app(env: str = "production") -> tuple[FastAPI, ShieldEngine]:
    engine = ShieldEngine(backend=MemoryBackend(), current_env=env)
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)
    return app, engine


# ---------------------------------------------------------------------------
# @deprecated decorator — metadata stamping
# ---------------------------------------------------------------------------


def test_deprecated_stamps_status():
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT", use_instead="/v2/users")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["status"] == "deprecated"


def test_deprecated_stamps_sunset():
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["sunset_date"] == "Sat, 01 Jan 2026 00:00:00 GMT"


def test_deprecated_stamps_successor_path():
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT", use_instead="/v2/users")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["successor_path"] == "/v2/users"


def test_deprecated_no_successor_when_omitted():
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["successor_path"] is None


async def test_deprecated_preserves_function():
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def my_endpoint():
        return {"ok": True}

    assert my_endpoint.__name__ == "my_endpoint"
    result = await my_endpoint()
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# @deprecated + ShieldRouter → state registered at startup
# ---------------------------------------------------------------------------


async def test_deprecated_registers_with_router():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/users")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT", use_instead="/v2/users")
    async def v1_users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    # @router.get() registers "GET:/v1/users" (method-specific key)
    state = await engine.backend.get_state("GET:/v1/users")
    assert state.status == RouteStatus.DEPRECATED
    assert state.sunset_date == "Sat, 01 Jan 2026 00:00:00 GMT"
    assert state.successor_path == "/v2/users"


# ---------------------------------------------------------------------------
# Middleware injects Deprecation/Sunset/Link headers
# ---------------------------------------------------------------------------


async def test_deprecated_route_still_returns_200():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/users")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def v1_users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/users")

    assert resp.status_code == 200


async def test_deprecated_injects_deprecation_header():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/users")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def v1_users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/users")

    assert resp.headers.get("deprecation") == "true"


async def test_deprecated_injects_sunset_header():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/users")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def v1_users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/users")

    assert "Sat, 01 Jan 2026" in resp.headers.get("sunset", "")


async def test_deprecated_injects_link_header_when_successor_set():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/users")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT", use_instead="/v2/users")
    async def v1_users():
        return {"users": []}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/users")

    link = resp.headers.get("link", "")
    assert "/v2/users" in link
    assert "successor-version" in link


async def test_deprecated_no_link_header_without_successor():
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/items")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def v1_items():
        return {}

    app.include_router(router)
    await app.router.startup()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/items")

    assert "link" not in resp.headers


async def test_active_route_has_no_deprecation_headers():
    """Non-deprecated routes must not get deprecation headers."""
    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v2/users")
    async def v2_users():
        return {"users": []}

    app.include_router(router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v2/users")

    assert "deprecation" not in resp.headers
    assert "sunset" not in resp.headers
    assert "link" not in resp.headers


async def test_deprecated_marked_in_openapi():
    """@deprecated route appears as deprecated:true in the OpenAPI schema."""
    from shield.fastapi.openapi import apply_shield_to_openapi

    app, engine = _build_app()
    router = ShieldRouter(engine=engine)

    @router.get("/v1/orders")
    @deprecated(sunset="Sat, 01 Jan 2026 00:00:00 GMT")
    async def v1_orders():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    get_op = schema["paths"].get("/v1/orders", {}).get("get", {})
    assert get_op.get("deprecated") is True
