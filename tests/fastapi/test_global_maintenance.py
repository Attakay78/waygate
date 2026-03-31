"""Tests for global maintenance mode.

Global maintenance blocks every route with a single engine call, without
requiring per-route decorators.  Options include:
- ``exempt_paths``          — specific routes that remain reachable
- ``include_force_active``  — override @force_active protection (default: False)
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fastapi._helpers import _trigger_startup
from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.fastapi.decorators import disabled, force_active
from waygate.fastapi.middleware import WaygateMiddleware
from waygate.fastapi.router import WaygateRouter


def _app_with_routes() -> tuple[FastAPI, WaygateEngine]:
    engine = WaygateEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    router = WaygateRouter(engine=engine)

    @router.get("/payments")
    async def payments():
        return {"ok": True}

    @router.get("/health")
    @force_active
    async def health():
        return {"status": "ok"}

    @router.get("/admin")
    @disabled(reason="Gone")
    async def admin():
        return {}

    app.include_router(router)
    return app, engine


# ---------------------------------------------------------------------------
# Engine — enable / disable / state
# ---------------------------------------------------------------------------


async def test_enable_global_maintenance_sets_config():
    engine = WaygateEngine(backend=MemoryBackend())
    cfg = await engine.enable_global_maintenance(reason="Planned downtime")
    assert cfg.enabled is True
    assert cfg.reason == "Planned downtime"


async def test_disable_global_maintenance_clears_config():
    engine = WaygateEngine(backend=MemoryBackend())
    await engine.enable_global_maintenance(reason="Downtime")
    cfg = await engine.disable_global_maintenance()
    assert cfg.enabled is False


async def test_get_global_maintenance_returns_disabled_by_default():
    engine = WaygateEngine(backend=MemoryBackend())
    cfg = await engine.get_global_maintenance()
    assert cfg.enabled is False


async def test_global_maintenance_persists_in_backend():
    engine = WaygateEngine(backend=MemoryBackend())
    await engine.enable_global_maintenance(reason="Persist test", exempt_paths=["/health"])
    cfg = await engine.get_global_maintenance()
    assert cfg.enabled is True
    assert "/health" in cfg.exempt_paths


async def test_global_maintenance_hidden_from_list_states():
    """The internal sentinel key must not appear in engine.list_states()."""
    engine = WaygateEngine(backend=MemoryBackend())
    await engine.enable_global_maintenance()
    states = await engine.list_states()
    paths = [s.path for s in states]
    assert not any(p.startswith("__waygate:") for p in paths)


async def test_global_maintenance_written_to_audit_log():
    engine = WaygateEngine(backend=MemoryBackend())
    await engine.enable_global_maintenance(reason="Audit test", actor="alice")
    log = await engine.get_audit_log()
    assert any(e.action == "global_maintenance_on" and e.actor == "alice" for e in log)

    await engine.disable_global_maintenance(actor="bob")
    log = await engine.get_audit_log()
    assert any(e.action == "global_maintenance_off" and e.actor == "bob" for e in log)


# ---------------------------------------------------------------------------
# Middleware — global maintenance blocks all routes
# ---------------------------------------------------------------------------


async def test_global_maintenance_blocks_normal_routes():
    app, engine = _app_with_routes()
    await engine.enable_global_maintenance(reason="System upgrade")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/payments")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MAINTENANCE_MODE"
    assert resp.json()["error"]["reason"] == "System upgrade"


async def test_global_maintenance_respects_force_active_by_default():
    """@force_active routes must remain reachable when include_force_active=False."""
    app, engine = _app_with_routes()
    await _trigger_startup(app)
    await engine.enable_global_maintenance(reason="System upgrade")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")

    assert resp.status_code == 200


async def test_global_maintenance_overrides_force_active_when_flag_set():
    """When include_force_active=True, even @force_active routes return 503."""
    app, engine = _app_with_routes()
    await _trigger_startup(app)
    await engine.enable_global_maintenance(reason="Hard lockdown", include_force_active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")

    assert resp.status_code == 503


async def test_global_maintenance_exempt_path_passes_through():
    app, engine = _app_with_routes()
    await engine.enable_global_maintenance(reason="System upgrade", exempt_paths=["/payments"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/payments")

    assert resp.status_code == 200


async def test_global_maintenance_method_specific_exempt_key():
    """Method-prefixed exempt keys (e.g. 'GET:/payments') work correctly."""
    app, engine = _app_with_routes()
    await engine.enable_global_maintenance(
        reason="Partial lockdown", exempt_paths=["GET:/payments"]
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        get_resp = await c.get("/payments")

    assert get_resp.status_code == 200


async def test_global_maintenance_disable_restores_normal_behaviour():
    app, engine = _app_with_routes()
    await engine.enable_global_maintenance(reason="Temp")
    await engine.disable_global_maintenance()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/payments")

    assert resp.status_code == 200


async def test_global_maintenance_also_applies_on_top_of_per_route_state():
    """Global maintenance takes priority over per-route ACTIVE state."""
    app, engine = _app_with_routes()
    # /payments has no decorator — its per-route state is ACTIVE.
    await engine.enable_global_maintenance(reason="Global wins")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/payments")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# set_global_exempt_paths
# ---------------------------------------------------------------------------


async def test_set_global_exempt_paths_replaces_list():
    engine = WaygateEngine(backend=MemoryBackend())
    await engine.enable_global_maintenance(exempt_paths=["/a", "/b"])
    updated = await engine.set_global_exempt_paths(["/c"])
    assert updated.exempt_paths == ["/c"]
    cfg = await engine.get_global_maintenance()
    assert cfg.exempt_paths == ["/c"]
