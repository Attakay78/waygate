"""Tests for maintenance-mode visual annotations in the OpenAPI schema
and the custom Swagger UI provided by setup_waygate_docs."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fastapi._helpers import _trigger_startup
from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.fastapi.decorators import maintenance
from waygate.fastapi.middleware import WaygateMiddleware
from waygate.fastapi.openapi import apply_waygate_to_openapi, setup_waygate_docs
from waygate.fastapi.router import WaygateRouter


def _build(env: str = "dev") -> tuple[FastAPI, WaygateEngine, WaygateRouter]:
    engine = WaygateEngine(backend=MemoryBackend(), current_env=env)
    router = WaygateRouter(engine=engine)
    app = FastAPI(title="Test API")
    app.add_middleware(WaygateMiddleware, engine=engine)
    return app, engine, router


# ---------------------------------------------------------------------------
# apply_waygate_to_openapi — maintenance schema annotations
# ---------------------------------------------------------------------------


async def test_maintenance_route_gets_x_waygate_status_extension():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    op = schema["paths"]["/payments"]["get"]
    assert op.get("x-waygate-status") == "maintenance"
    assert op.get("x-waygate-reason") == "DB migration"


async def test_maintenance_route_description_contains_warning_banner():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="Scheduled window")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    desc = schema["paths"]["/payments"]["get"].get("description", "")
    assert "🔧" in desc
    assert "Scheduled window" in desc
    # Must be a markdown blockquote so both Swagger UI and ReDoc render it.
    assert desc.startswith(">")


async def test_maintenance_route_summary_prefixed_with_wrench():
    app, engine, router = _build()

    @router.get("/payments", summary="Get payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    summary = schema["paths"]["/payments"]["get"].get("summary", "")
    assert summary.startswith("🔧")
    assert "Get payments" in summary


async def test_maintenance_route_summary_prefix_not_doubled():
    """Calling openapi() twice must not double the '🔧' prefix."""
    app, engine, router = _build()

    @router.get("/payments", summary="Get payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    _ = app.openapi()
    schema2 = app.openapi()
    summary = schema2["paths"]["/payments"]["get"].get("summary", "")
    assert summary.count("🔧") == 1, f"Prefix doubled: {summary!r}"


async def test_maintenance_route_remains_visible_in_schema():
    """Unlike DISABLED routes, maintenance routes must still appear in docs."""
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    assert "/payments" in schema["paths"]


async def test_existing_description_preserved_after_banner():
    """The original operation description must follow the warning banner."""
    app, engine, router = _build()

    @router.get("/payments", description="Returns all payments.")
    @maintenance(reason="Upgrade")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)

    schema = app.openapi()
    desc = schema["paths"]["/payments"]["get"].get("description", "")
    assert "Returns all payments." in desc
    # Banner must come before the original description.
    assert desc.index("🔧") < desc.index("Returns all payments.")


# ---------------------------------------------------------------------------
# setup_waygate_docs — custom Swagger UI with maintenance CSS/JS injected
# ---------------------------------------------------------------------------


async def test_setup_waygate_docs_serves_html():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/docs")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_setup_waygate_docs_injects_maintenance_script():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/docs")

    html = resp.text
    # Verify the injected JS payload is present.
    assert "x-waygate-status" in html
    assert "waygate-maintenance-block" in html
    assert "waygate-maintenance-badge" in html
    assert "🔧 MAINTENANCE" in html


async def test_setup_waygate_docs_embeds_openapi_url():
    app, engine, router = _build()

    app.include_router(router)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/docs")

    # The data attribute must be present so the injected JS can find the
    # openapi URL without hard-coding it.
    assert 'data-openapi-url="/openapi.json"' in resp.text


async def test_setup_waygate_docs_does_not_break_normal_routes():
    """After setup_waygate_docs, normal API routes must still work."""
    app, engine, router = _build()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(router)
    await _trigger_startup(app)
    apply_waygate_to_openapi(app, engine)
    setup_waygate_docs(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
