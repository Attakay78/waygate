"""ShieldRouter — drop-in replacement for FastAPI's APIRouter.

At application startup it scans every route for ``__shield_meta__``
and calls ``engine.register()`` so that the engine's backend reflects the
decorator-declared state.

``scan_routes()`` is a standalone helper that does the same scan for any
FastAPI/Starlette app — allowing plain ``APIRouter`` and routes registered
directly on the ``FastAPI`` app to benefit from shield decorators too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from starlette.routing import Route

from shield.core.engine import ShieldEngine


async def scan_routes(app: Any, engine: ShieldEngine) -> None:
    """Scan *app*'s routes and register them with *engine*.

    Works with any ``FastAPI`` or ``Starlette`` application — including routes
    defined on plain ``APIRouter`` instances and routes added directly to the
    ``FastAPI`` app.

    **All routes are registered**, not just decorated ones:

    - Routes with ``__shield_meta__`` are registered with their decorator state.
    - Routes without ``__shield_meta__`` are registered as ``ACTIVE``.

    This ensures the backend is the definitive record of every route that
    exists in the application.  The CLI uses this to distinguish between a
    real undecorated route (registered as ``ACTIVE`` — mutable via CLI) and a
    path that does not exist (not in backend — CLI raises an error).

    This function is **idempotent**: routes already registered (e.g. by a
    ``ShieldRouter`` startup hook or a previous ``scan_routes()`` call) are
    left untouched because ``engine.register_batch()`` honours persisted state.

    Uses ``engine.register_batch()`` to discover all already-persisted routes
    in one backend call instead of N individual ``get_state()`` reads.

    Parameters
    ----------
    app:
        A ``FastAPI`` or ``Starlette`` application instance.
        Inside ``ShieldMiddleware`` this is ``request.app``.
    engine:
        The ``ShieldEngine`` that owns all route state.
    """
    routes_to_register: list[tuple[str, dict[str, Any]]] = []

    for route in getattr(app, "routes", []):
        if not isinstance(route, Route):
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        # Skip FastAPI's built-in docs/schema routes — they are not
        # user-defined routes and should never appear in shield status.
        if route.path in {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}:
            continue
        # Use decorator meta if present; fall back to empty dict for
        # undecorated routes so they are still registered as ACTIVE.
        meta: dict[str, Any] = getattr(endpoint, "__shield_meta__", {})
        methods: set[str] = route.methods or set()
        if methods:
            for method in sorted(methods):
                routes_to_register.append((f"{method}:{route.path}", meta))
        else:
            routes_to_register.append((route.path, meta))

    await engine.register_batch(routes_to_register)


class ShieldRouter(APIRouter):
    """Drop-in replacement for ``fastapi.APIRouter``.

    Identical to ``APIRouter`` in every way except that it registers
    shield metadata with the engine at startup.

    Parameters
    ----------
    engine:
        The ``ShieldEngine`` instance shared with the middleware.
    **kwargs:
        All other keyword arguments are forwarded to ``APIRouter``.
    """

    def __init__(self, engine: ShieldEngine, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shield_engine = engine
        # Collect (path, meta) pairs discovered via add_api_route.
        self._shield_routes: list[tuple[str, dict[str, Any]]] = []
        # Register the startup hook so FastAPI picks it up automatically.
        self.on_startup.append(self.register_shield_routes)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_engine(cls, engine: ShieldEngine, **kwargs: Any) -> ShieldRouter:
        """Create a ``ShieldRouter`` bound to *engine*."""
        return cls(engine=engine, **kwargs)

    # ------------------------------------------------------------------
    # Override route registration to detect __shield_meta__
    # ------------------------------------------------------------------

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        """Register a route and record any shield metadata on the endpoint.

        When *methods* are provided (e.g. from ``@router.get()``), each HTTP
        method gets its own state key — ``"GET:/payments"``, ``"POST:/payments"``
        — so they can be controlled independently via the CLI or engine.

        When no methods are specified (rare), the bare path ``"/payments"`` is
        used as an all-methods fallback key.

        The router's ``prefix`` is prepended to *path* so that the registered
        key matches the full path that appears in ``app.routes`` and that
        ``ShieldMiddleware`` resolves via template-path lookup.  For example,
        a router with ``prefix="/api"`` and a route ``"/payments"`` produces
        the key ``"GET:/api/payments"``, not ``"GET:/payments"``.
        """
        super().add_api_route(path, endpoint, **kwargs)

        # Use decorator meta if present; fall back to empty dict for
        # undecorated routes so they are still registered as ACTIVE.
        meta: dict[str, Any] = getattr(endpoint, "__shield_meta__", {})

        # Prepend the router prefix so the key aligns with the full app path.
        full_path = (self.prefix or "") + path

        methods: set[str] = {m.upper() for m in (kwargs.get("methods") or [])}
        if methods:
            # Register one state key per HTTP method so each can be
            # controlled independently: GET:/payments, POST:/payments, etc.
            for method in sorted(methods):
                self._shield_routes.append((f"{method}:{full_path}", meta))
        else:
            # No methods known — fall back to path-level (all-methods) key.
            self._shield_routes.append((full_path, meta))

    # ------------------------------------------------------------------
    # Startup: register all discovered routes with the engine
    # ------------------------------------------------------------------

    async def register_shield_routes(self) -> None:
        """Register all shield-decorated routes with the engine.

        Call this during application startup (e.g. via a ``lifespan``
        handler or ``on_startup`` event).  ``ShieldRouter`` calls this
        automatically when you pass it to ``app.include_router()``.

        Uses ``engine.register_batch()`` — a single ``list_states()`` backend
        call discovers all already-persisted routes, then only the truly new
        routes are written.  For ``FileBackend`` this means one file read and
        one debounced file write instead of N reads and N writes.

        Also calls ``engine.start()`` to launch any background tasks needed
        for distributed operation (e.g. the global config cache-invalidation
        listener when using ``RedisBackend``).
        """
        await self._shield_engine.register_batch(list(self._shield_routes))
        await self._shield_engine.start()

    # ------------------------------------------------------------------
    # Hook into include_router so startup fires automatically
    # ------------------------------------------------------------------

    def _get_startup_handler(self) -> Callable[[], Any]:
        """Return an async startup handler that registers shield routes."""

        async def _startup() -> None:
            await self.register_shield_routes()

        return _startup
