"""Tests: global maintenance mode in OpenAPI schema and docs UI."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fastapi._helpers import _trigger_startup
from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.fastapi.decorators import force_active, maintenance
from waygate.fastapi.middleware import WaygateMiddleware
from waygate.fastapi.openapi import apply_waygate_to_openapi, setup_waygate_docs
from waygate.fastapi.router import WaygateRouter


def _build() -> tuple[FastAPI, WaygateEngine, WaygateRouter]:
    engine = WaygateEngine(backend=MemoryBackend(), current_env="production")
    router = WaygateRouter(engine=engine)
    app = FastAPI(title="Test API")
    app.add_middleware(WaygateMiddleware, engine=engine)
    return app, engine, router


# ---------------------------------------------------------------------------
# Schema — x-waygate-global-maintenance extension in info
# ---------------------------------------------------------------------------


async def test_schema_has_global_maintenance_extension_when_off():
    app, engine, router = _build()

    @router.get("/payments")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    gm = schema["info"].get("x-waygate-global-maintenance", {})
    assert gm.get("enabled") is False


async def test_schema_has_global_maintenance_extension_when_on():
    app, engine, router = _build()

    @router.get("/payments")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    await engine.enable_global_maintenance(reason="Deploy window")
    schema = app.openapi()
    gm = schema["info"]["x-waygate-global-maintenance"]
    assert gm["enabled"] is True
    assert gm["reason"] == "Deploy window"


async def test_schema_info_has_extension_not_description_when_on():
    """The global maintenance notice lives in x-waygate-global-maintenance, not
    in info.description, to avoid a duplicate banner alongside the HTML one."""
    app, engine, router = _build()

    @router.get("/payments")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)
    await engine.enable_global_maintenance(reason="Emergency patch")

    schema = app.openapi()
    # Extension field carries the data — description is left untouched.
    gm = schema["info"]["x-waygate-global-maintenance"]
    assert gm["enabled"] is True
    assert gm["reason"] == "Emergency patch"
    # info.description must NOT contain a "SITE-WIDE MAINTENANCE" duplicate.
    desc = schema["info"].get("description", "")
    assert "SITE-WIDE MAINTENANCE" not in desc


async def test_schema_operations_annotated_with_global_maintenance():
    """Non-exempt operations must get x-waygate-status=maintenance when global is ON."""
    app, engine, router = _build()

    @router.get("/orders")
    async def orders():
        return {}

    @router.get("/health")
    @force_active
    async def health():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    await engine.enable_global_maintenance(reason="Upgrade", exempt_paths=["/health"])
    schema = app.openapi()

    # /orders is not exempt — must be annotated.
    orders_op = schema["paths"]["/orders"]["get"]
    assert orders_op.get("x-waygate-status") == "maintenance"
    assert orders_op["summary"].startswith("🔧")

    # /health is exempt — must NOT be annotated.
    health_op = schema["paths"]["/health"]["get"]
    assert health_op.get("x-waygate-status") != "maintenance"


async def test_schema_per_route_maintenance_not_overwritten_by_global():
    """A route already in per-route maintenance keeps its own annotation."""
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    await engine.enable_global_maintenance(reason="Global reason")
    schema = app.openapi()
    op = schema["paths"]["/payments"]["get"]
    # Per-route reason wins.
    assert op.get("x-waygate-reason") == "DB migration"


async def test_schema_global_maintenance_removed_after_disable():
    app, engine, router = _build()

    @router.get("/payments")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    await engine.enable_global_maintenance(reason="Temp")
    await engine.disable_global_maintenance()

    schema = app.openapi()
    gm = schema["info"]["x-waygate-global-maintenance"]
    assert gm["enabled"] is False
    # Operation must not be annotated after disabling.
    op = schema["paths"]["/payments"]["get"]
    assert op.get("x-waygate-status") != "maintenance"


# ---------------------------------------------------------------------------
# setup_waygate_docs — HTML injection for global banner + ok chip
# ---------------------------------------------------------------------------


async def test_setup_waygate_docs_injects_global_banner_script():
    app, engine, router = _build()
    app.include_router(router)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")

    for resp in (docs, redoc):
        assert resp.status_code == 200
        html = resp.text
        assert "waygate-global-banner" in html
        assert "waygate-ok-chip" in html
        assert "All systems operational" in html
        assert "Site-Wide Maintenance" in html


async def test_setup_waygate_docs_both_endpoints_respond():
    app, engine, router = _build()
    app.include_router(router)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        docs_resp = await client.get("/docs")
        redoc_resp = await client.get("/redoc")

    assert docs_resp.status_code == 200
    assert "text/html" in docs_resp.headers["content-type"]
    assert redoc_resp.status_code == 200
    assert "text/html" in redoc_resp.headers["content-type"]


async def test_setup_waygate_docs_polling_script_present():
    """The 15-second spec polling loop must be in the injected script."""
    app, engine, router = _build()
    app.include_router(router)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        html = (await client.get("/docs")).text

    assert "POLL_INTERVAL_MS" in html
    assert "setInterval" in html
