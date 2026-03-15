"""Tests for shield.fastapi.openapi — OpenAPI schema filtering."""

from __future__ import annotations

from fastapi import FastAPI

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.fastapi.decorators import disabled, env_only, maintenance
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.openapi import apply_shield_to_openapi
from shield.fastapi.router import ShieldRouter


def _make_full_app(env: str = "dev"):
    engine = ShieldEngine(backend=MemoryBackend(), current_env=env)
    router = ShieldRouter(engine=engine)
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)
    return app, engine, router


# ---------------------------------------------------------------------------
# DISABLED routes are hidden from schema
# ---------------------------------------------------------------------------


async def test_disabled_route_hidden_from_openapi():
    app, engine, router = _make_full_app()

    @router.get("/old")
    @disabled(reason="gone")
    async def old():
        return {}

    @router.get("/new")
    async def new():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/old" not in schema["paths"]
    assert "/new" in schema["paths"]


# ---------------------------------------------------------------------------
# ENV_GATED routes are hidden in the wrong environment
# ---------------------------------------------------------------------------


async def test_env_gated_route_hidden_in_wrong_env():
    app, engine, router = _make_full_app(env="production")

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {}

    @router.get("/health")
    async def health():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/debug" not in schema["paths"]
    assert "/health" in schema["paths"]


async def test_env_gated_route_visible_in_correct_env():
    app, engine, router = _make_full_app(env="dev")

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/debug" in schema["paths"]


# ---------------------------------------------------------------------------
# DEPRECATED routes are marked deprecated: true
# ---------------------------------------------------------------------------


async def test_deprecated_route_marked_in_schema():
    app, engine, router = _make_full_app()

    @router.get("/v1/users")
    async def v1_users():
        return {}

    app.include_router(router)
    # Manually put the route into DEPRECATED state using its method-specific key.
    await engine.backend.set_state(
        "GET:/v1/users",
        (await engine.get_state("GET:/v1/users")).model_copy(
            update={"status": "deprecated", "path": "GET:/v1/users"}
        ),
    )
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/v1/users" in schema["paths"]
    get_op = schema["paths"]["/v1/users"].get("get", {})
    assert get_op.get("deprecated") is True


# ---------------------------------------------------------------------------
# MAINTENANCE routes still appear in the schema (not hidden)
# ---------------------------------------------------------------------------


async def test_maintenance_route_still_in_schema():
    app, engine, router = _make_full_app()

    @router.get("/payments")
    @maintenance(reason="DB")
    async def payments():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    # Maintenance routes are blocked at request time but still visible in docs.
    assert "/payments" in schema["paths"]


# ---------------------------------------------------------------------------
# Unregistered routes are untouched
# ---------------------------------------------------------------------------


async def test_unregistered_route_passes_through():
    app, engine, router = _make_full_app()

    @router.get("/api/products")
    async def products():
        return {}

    app.include_router(router)
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/api/products" in schema["paths"]


# ---------------------------------------------------------------------------
# Regression: schema must reflect live state changes, not be cached stale
# ---------------------------------------------------------------------------


async def test_openapi_reflects_state_change_without_restart():
    """Bug regression: disabling a route after /docs was hit must hide it.

    The old implementation mutated ``self.openapi_schema`` in-place, so a
    route removed in one call was permanently gone from the cache.  Equally,
    re-enabling a hidden route never showed it again.
    """
    app, engine, router = _make_full_app()

    @router.get("/old")
    @disabled(reason="gone")
    async def old():
        return {}

    @router.get("/new")
    async def new():
        return {}

    app.include_router(router)
    await app.router.startup()
    apply_shield_to_openapi(app, engine)

    # First call — /old is disabled and should be hidden.
    schema1 = app.openapi()
    assert "/old" not in schema1["paths"]
    assert "/new" in schema1["paths"]

    # Re-enable /old via engine using the method-specific key.
    await engine.enable("GET:/old")

    # Second call — /old must now appear without restarting.
    schema2 = app.openapi()
    assert "/old" in schema2["paths"], (
        "/old was re-enabled but still absent from schema — cache mutation bug"
    )

    # Third call: disable /new at runtime (was never decorated).
    # Must use the method-prefixed key since startup registers GET:/new.
    await engine.disable("GET:/new")
    schema3 = app.openapi()
    assert "/new" not in schema3["paths"], (
        "/new was disabled at runtime but still in schema — cache mutation bug"
    )
