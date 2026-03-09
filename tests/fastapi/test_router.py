"""Tests for ShieldRouter."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import RouteStatus
from shield.fastapi.decorators import disabled, env_only, maintenance
from shield.fastapi.router import ShieldRouter


@pytest.fixture
def engine() -> ShieldEngine:
    return ShieldEngine(backend=MemoryBackend(), current_env="production")


@pytest.fixture
def router(engine) -> ShieldRouter:
    return ShieldRouter(engine=engine)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_router_registers_maintenance_route(engine, router):
    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments():
        return {"ok": True}

    await router.register_shield_routes()

    # @router.get() → method-specific key "GET:/payments"
    state = await engine.backend.get_state("GET:/payments")
    assert state.status == RouteStatus.MAINTENANCE
    assert state.reason == "DB migration"


async def test_router_registers_env_gated_route(engine, router):
    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {"env": "dev"}

    await router.register_shield_routes()

    state = await engine.backend.get_state("GET:/debug")
    assert state.status == RouteStatus.ENV_GATED
    assert state.allowed_envs == ["dev"]


async def test_router_registers_disabled_route(engine, router):
    @router.get("/old")
    @disabled(reason="gone")
    async def old():
        return {}

    await router.register_shield_routes()

    state = await engine.backend.get_state("GET:/old")
    assert state.status == RouteStatus.DISABLED


async def test_router_registers_undecorated_routes_as_active(engine, router):
    @router.get("/health")
    async def health():
        return {"status": "ok"}

    await router.register_shield_routes()

    # Undecorated routes are registered as ACTIVE so the CLI can
    # validate that a path actually exists in the application.
    state = await engine.backend.get_state("GET:/health")
    assert state.status.value == "active"


async def test_from_engine_factory(engine):
    router = ShieldRouter.from_engine(engine)
    assert router._shield_engine is engine


# ---------------------------------------------------------------------------
# Startup hook fires automatically via app lifecycle
# ---------------------------------------------------------------------------


async def test_startup_registers_routes_via_app_lifespan(engine):
    """include_router forwards on_startup; triggering app startup registers routes."""
    router = ShieldRouter(engine=engine)

    @router.get("/pay")
    @maintenance(reason="test maint")
    async def pay():
        return {}

    app = FastAPI()
    app.include_router(router)

    # Trigger the app's startup events directly (equivalent to server startup).
    await app.router.startup()

    state = await engine.backend.get_state("GET:/pay")
    assert state.status == RouteStatus.MAINTENANCE
