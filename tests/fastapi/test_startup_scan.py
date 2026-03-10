"""Tests for eager route scanning at ASGI lifespan startup.

Verifies the two issues that existed when using only plain ``APIRouter``
(no ``ShieldRouter``):

1. **CLI shows no routes** — state was never written to the backend until
   the first HTTP request.  The fix: ``ShieldMiddleware.__call__`` intercepts
   ``lifespan.startup.complete`` and runs ``scan_routes()`` immediately after
   the app's own startup events (e.g. ``ShieldRouter.on_startup``) have fired.

2. **OpenAPI schema unfiltered** — ``apply_shield_to_openapi`` reads state from
   the engine.  If state was not registered (no request had been made), all
   routes appeared in ``/docs`` regardless of their decorator.  The fix:
   ``_ensure_routes_scanned`` is now called on EVERY request (including
   ``/openapi.json``) as a fallback.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import RouteStatus
from shield.fastapi.decorators import deprecated, disabled, env_only, maintenance
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.openapi import apply_shield_to_openapi
from shield.fastapi.router import ShieldRouter

# ---------------------------------------------------------------------------
# Helper: simulate ASGI lifespan startup
#
# httpx.ASGITransport does NOT trigger the ASGI lifespan protocol, so we
# drive it manually.  Starlette.__call__ sets scope["app"] = self before
# invoking the middleware stack — our interception relies on this.
# ---------------------------------------------------------------------------


async def _lifespan_startup(app: FastAPI) -> None:
    """Drive the ASGI lifespan startup for *app* and return after completion.

    Starlette sets ``scope["app"] = app`` inside its ``__call__`` before
    delegating to the middleware stack, so by the time
    ``ShieldMiddleware.__call__`` runs, ``scope["app"]`` is the FastAPI app.
    """
    startup_complete: asyncio.Event = asyncio.Event()
    call_count = 0

    async def receive() -> dict[str, Any]:
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return {"type": "lifespan.startup"}
        # Block indefinitely — we cancel the task once startup is done.
        await asyncio.sleep(3600)
        return {}  # unreachable

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "lifespan.startup.complete":
            startup_complete.set()

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}

    task = asyncio.create_task(app(scope, receive, send))
    await startup_complete.wait()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass  # expected on cancel; swallow cleanup noise


# ---------------------------------------------------------------------------
# Eager scan via ASGI lifespan interception
# ---------------------------------------------------------------------------


async def test_plain_router_state_registered_at_lifespan_before_any_request():
    """Routes on a plain APIRouter must appear in the backend after lifespan
    startup — without any HTTP request having been made.

    This is the CLI scenario: the CLI talks directly to the backend and would
    show no routes if we only scanned lazily on HTTP requests.
    """
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = APIRouter()

    @router.get("/catalog/products")
    @disabled(reason="Retired")
    async def products():
        return {}

    @router.get("/catalog/items")
    @maintenance(reason="Rebuilding")
    async def items():
        return {}

    app.include_router(router)

    # No HTTP requests made yet — only ASGI lifespan startup triggered.
    await _lifespan_startup(app)

    states = await engine.list_states()
    paths = {s.path for s in states}

    assert "GET:/catalog/products" in paths
    assert "GET:/catalog/items" in paths

    prod_state = await engine.backend.get_state("GET:/catalog/products")
    assert prod_state.status == RouteStatus.DISABLED

    item_state = await engine.backend.get_state("GET:/catalog/items")
    assert item_state.status == RouteStatus.MAINTENANCE


async def test_lifespan_scan_is_idempotent_with_shield_router():
    """When ShieldRouter coexists with plain APIRouter, the lifespan scan must
    not double-register ShieldRouter routes (engine.register is idempotent,
    but state must not be reset to decorator default after a runtime change)."""
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    shield_router = ShieldRouter(engine=engine)

    @shield_router.get("/payments")
    @maintenance(reason="DB migration")
    async def payments():
        return {}

    plain_router = APIRouter()

    @plain_router.get("/orders")
    @disabled(reason="Retired")
    async def orders():
        return {}

    app.include_router(shield_router)
    app.include_router(plain_router)

    # ShieldRouter startup hook fires first (via app.router.startup).
    await app.router.startup()

    # Simulate a runtime state change — engine.enable overrides the decorator.
    await engine.enable("GET:/payments")
    enabled_state = await engine.backend.get_state("GET:/payments")
    assert enabled_state.status == RouteStatus.ACTIVE

    # Now run lifespan startup — the middleware's scan runs here.
    await _lifespan_startup(app)

    # ShieldRouter route must NOT be reset to MAINTENANCE by the scan.
    post_scan_state = await engine.backend.get_state("GET:/payments")
    assert post_scan_state.status == RouteStatus.ACTIVE, (
        "Lifespan scan reset runtime state — engine.register idempotency broken"
    )

    # Plain router route must now be registered.
    orders_state = await engine.backend.get_state("GET:/orders")
    assert orders_state.status == RouteStatus.DISABLED


# ---------------------------------------------------------------------------
# Fallback scan for environments without ASGI lifespan (e.g. tests using
# httpx + ASGITransport which do not drive the lifespan protocol)
# ---------------------------------------------------------------------------


async def test_openapi_request_triggers_fallback_scan():
    """/openapi.json must trigger the lazy scan so the schema is filtered
    even when the ASGI lifespan was not used (e.g. httpx test transport)."""
    engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = APIRouter()

    @router.get("/secret")
    @env_only("dev")
    async def secret():
        return {}

    @router.get("/public")
    async def public():
        return {}

    app.include_router(router)
    apply_shield_to_openapi(app, engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # First request is to /openapi.json — no route request has been made.
        resp = await client.get("/openapi.json")

    assert resp.status_code == 200
    schema = resp.json()
    # /secret is ENV_GATED in production — must be hidden.
    assert "/secret" not in schema["paths"]
    assert "/public" in schema["paths"]


# ---------------------------------------------------------------------------
# OpenAPI filtering with plain APIRouter (no ShieldRouter at all)
# ---------------------------------------------------------------------------


async def test_openapi_hides_disabled_plain_router_route_after_lifespan():
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = APIRouter()

    @router.get("/legacy")
    @disabled(reason="Use /v2/legacy")
    async def legacy():
        return {}

    @router.get("/active")
    async def active():
        return {}

    app.include_router(router)

    await _lifespan_startup(app)
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/legacy" not in schema["paths"], "/legacy is DISABLED — must be hidden"
    assert "/active" in schema["paths"]


async def test_openapi_hides_env_gated_plain_router_route_after_lifespan():
    engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = APIRouter()

    @router.get("/debug")
    @env_only("dev")
    async def debug():
        return {}

    app.include_router(router)

    await _lifespan_startup(app)
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/debug" not in schema["paths"], (
        "/debug is ENV_GATED (dev-only) — must be hidden in production"
    )


async def test_openapi_marks_deprecated_plain_router_route_after_lifespan():
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    router = APIRouter()

    @router.get("/v1/items")
    @deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/items")
    async def v1_items():
        return {}

    app.include_router(router)

    await _lifespan_startup(app)
    apply_shield_to_openapi(app, engine)

    schema = app.openapi()
    assert "/v1/items" in schema["paths"]
    op = schema["paths"]["/v1/items"].get("get", {})
    assert op.get("deprecated") is True


# ---------------------------------------------------------------------------
# Verify scope["app"] is set correctly during lifespan (Starlette guarantee)
# ---------------------------------------------------------------------------


async def test_scope_app_is_set_during_lifespan():
    """Confirm that Starlette sets scope['app'] before the middleware stack
    is invoked — this is the mechanism ShieldMiddleware relies on."""
    captured_app: list[Any] = []

    from starlette.types import Receive, Scope, Send

    class CapturingMiddleware(ShieldMiddleware):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "lifespan":
                captured_app.append(scope.get("app"))
            await super().__call__(scope, receive, send)

    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(CapturingMiddleware, engine=engine)

    await _lifespan_startup(app)

    assert len(captured_app) == 1, "Lifespan __call__ not invoked"
    assert captured_app[0] is app, (
        f"scope['app'] was {captured_app[0]!r}, expected the FastAPI app"
    )
