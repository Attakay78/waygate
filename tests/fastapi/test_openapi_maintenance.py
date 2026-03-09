"""Tests for maintenance-mode visual annotations in the OpenAPI schema
and the custom Swagger UI provided by setup_shield_docs."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.fastapi.decorators import maintenance
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.openapi import apply_shield_to_openapi, setup_shield_docs
from shield.fastapi.router import ShieldRouter


def _build(env: str = "production") -> tuple[FastAPI, ShieldEngine, ShieldRouter]:
    engine = ShieldEngine(backend=MemoryBackend(), current_env=env)
    router = ShieldRouter(engine=engine)
    app = FastAPI(title="Test API")
    app.add_middleware(ShieldMiddleware, engine=engine)
    return app, engine, router


# ---------------------------------------------------------------------------
# apply_shield_to_openapi — maintenance schema annotations
# ---------------------------------------------------------------------------


async def test_maintenance_route_gets_x_shield_status_extension():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    op = schema["paths"]["/payments"]["get"]
    assert op.get("x-shield-status") == "maintenance"
    assert op.get("x-shield-reason") == "DB migration"


async def test_maintenance_route_description_contains_warning_banner():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="Scheduled window")
    async def payments():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

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
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

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
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

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
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

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
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    desc = schema["paths"]["/payments"]["get"].get("description", "")
    assert "Returns all payments." in desc
    # Banner must come before the original description.
    assert desc.index("🔧") < desc.index("Returns all payments.")


# ---------------------------------------------------------------------------
# setup_shield_docs — custom Swagger UI with maintenance CSS/JS injected
# ---------------------------------------------------------------------------


async def test_setup_shield_docs_serves_html():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)
    setup_shield_docs(app, engine)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/docs")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_setup_shield_docs_injects_maintenance_script():
    app, engine, router = _build()

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)
    setup_shield_docs(app, engine)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/docs")

    html = resp.text
    # Verify the injected JS payload is present.
    assert "x-shield-status" in html
    assert "shield-maintenance-block" in html
    assert "shield-maintenance-badge" in html
    assert "🔧 MAINTENANCE" in html


async def test_setup_shield_docs_embeds_openapi_url():
    app, engine, router = _build()

    app.include_router(router)
    apply_shield_to_openapi(app, engine)
    setup_shield_docs(app, engine)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/docs")

    # The data attribute must be present so the injected JS can find the
    # openapi URL without hard-coding it.
    assert 'data-openapi-url="/openapi.json"' in resp.text


async def test_setup_shield_docs_does_not_break_normal_routes():
    """After setup_shield_docs, normal API routes must still work."""
    app, engine, router = _build()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)
    setup_shield_docs(app, engine)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
