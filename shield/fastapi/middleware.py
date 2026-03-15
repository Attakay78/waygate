"""ShieldMiddleware — ASGI middleware that enforces route lifecycle policies.

Every inbound request passes through ``dispatch()``.  The middleware:

1. Scans all app routes for ``__shield_meta__`` at ASGI lifespan startup so
   that state is available to the CLI and OpenAPI schema before the first
   HTTP request.
2. Skips the check for dashboard, docs, and ``force_active`` routes.
3. Calls ``engine.check(path)``.
4. Translates ``ShieldException`` subclasses into structured JSON responses.
5. Injects RFC headers for deprecated routes and ``Retry-After`` for
   maintenance responses.

Route scanning — how and when it happens
-----------------------------------------
``ShieldMiddleware`` hooks into the ASGI lifespan to scan *all* app routes for
``__shield_meta__`` immediately after the server's startup events complete
(which means ``ShieldRouter.on_startup`` hooks have already run).

``scope["app"]`` is set by ``Starlette.__call__`` before the middleware stack
is invoked, for *all* scope types including ``"lifespan"``.  This gives the
middleware access to the FastAPI app — and therefore to ``app.routes`` — at
startup time without any additional configuration from the user.

The scan is also repeated lazily on the first HTTP dispatch as a safety net
for environments where the ASGI lifespan is not used.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from shield.core.engine import ShieldEngine
from shield.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from shield.core.models import RouteStatus

# Prefixes that are always exempt from shield checks.
_SKIP_PREFIXES = ("/shield/", "/docs", "/redoc", "/openapi.json")


class ShieldMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces route lifecycle policies.

    Parameters
    ----------
    app:
        The ASGI application to wrap.
    engine:
        The ``ShieldEngine`` that owns all route state.
    responses:
        Optional mapping of shield status → response factory used as the
        **global default** when a route has no per-route ``response=``
        factory set on its decorator.

        Supported keys:

        * ``"maintenance"`` — called when a route is in maintenance mode or
          global maintenance is active.
        * ``"disabled"``    — called when a route is permanently disabled.
        * ``"env_gated"``   — called when a route is accessed from the wrong
          environment.

        Each value must be a sync or async callable with the signature
        ``(request: Request, exc: Exception) -> Response``.

        Resolution order per request: **per-route factory** (``response=``
        on the decorator) → **global default** (this dict) → **built-in
        JSON error response**.

        Example::

            from starlette.responses import HTMLResponse

            def maintenance_page(request, exc):
                return HTMLResponse(
                    f"<h1>Down for maintenance</h1><p>{exc.reason}</p>",
                    status_code=503,
                )

            app.add_middleware(
                ShieldMiddleware,
                engine=engine,
                responses={
                    "maintenance": maintenance_page,
                    "disabled": lambda req, exc: HTMLResponse(
                        "<h1>This page is gone</h1>", status_code=503
                    ),
                },
            )
    """

    def __init__(
        self,
        app: ASGIApp,
        engine: ShieldEngine,
        responses: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(app)
        self.engine = engine
        self._default_responses: dict[str, Any] = responses or {}
        self._scan_lock: asyncio.Lock = asyncio.Lock()
        self._routes_scanned: bool = False
        # Pre-built route lookup cache — populated after scan_routes() completes.
        # Static paths (no path params) get an O(1) dict lookup.
        # Parameterised paths fall back to a short list scan (usually << total routes).
        # Each entry stores (is_force_active, template_path, response_factory | None).
        self._static_route_meta: dict[str, tuple[bool, str, Any]] = {}
        self._param_routes: list[tuple[Route, bool, str, Any]] = []
        self._route_cache_built: bool = False

    # ------------------------------------------------------------------
    # ASGI entry point — intercept lifespan for eager startup scan
    # ------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point.

        For ``lifespan`` scopes, wraps *send* to intercept the
        ``lifespan.startup.complete`` message.  At that point all startup
        event handlers (including ``ShieldRouter.on_startup``) have already
        run, so we can safely scan the remaining plain-router routes and
        register them with the engine before the server begins serving
        requests.

        ``scope["app"]`` is set by ``Starlette.__call__`` for every scope
        type, giving us access to the FastAPI app and its routes.
        """
        if scope["type"] == "lifespan":
            app = scope.get("app")
            if app is not None:
                original_send = send

                async def _send(message: Any) -> None:
                    if message.get("type") == "lifespan.startup.complete":
                        # Scan routes and start the scheduler polling loop
                        # after all startup events (ShieldRouter hooks, etc.)
                        # have already run.
                        await self._do_scan(app)
                        self.engine.scheduler.start_polling()
                        # Make the engine discoverable by decorator deps via
                        # request.app.state.shield_engine — this is what lets
                        # maintenance()/disabled()/env_only() find the engine
                        # without needing engine= on every call.
                        from shield.fastapi.dependencies import configure_shield

                        configure_shield(app, self.engine)
                    elif message.get("type") == "lifespan.shutdown.complete":
                        # Clean up the polling task on graceful shutdown.
                        self.engine.scheduler.stop_polling()
                    await original_send(message)

                await self.app(scope, receive, _send)
                return

        await super().__call__(scope, receive, send)

    # ------------------------------------------------------------------
    # Route scanning helpers
    # ------------------------------------------------------------------

    async def _do_scan(self, app: Any) -> None:
        """Run ``scan_routes`` exactly once, protected by a lock."""
        async with self._scan_lock:
            if self._routes_scanned:
                return
            from shield.fastapi.router import scan_routes

            await scan_routes(app, self.engine)
            self._routes_scanned = True
            # Build the O(1) route-lookup cache now that all routes are registered.
            self._build_route_cache(app)

    def _build_route_cache(self, app: Any) -> None:
        """Pre-build a fast route-metadata lookup structure.

        Splits app routes into two buckets:

        * ``_static_route_meta`` — exact-path routes (no ``{params}``).
          Resolved in O(1) via dict lookup on every request.
        * ``_param_routes`` — parameterised routes (e.g. ``/items/{id}``).
          Stored as a short list; still requires ``route.matches()`` but
          the list is typically much smaller than the total route count.

        The structure stores ``(is_force_active, template_path)`` per route
        so ``_resolve_route`` can answer both questions in a single pass.
        """
        static: dict[str, tuple[bool, str, Any]] = {}
        param: list[tuple[Route, bool, str, Any]] = []

        for route in getattr(app, "routes", []):
            if not isinstance(route, Route):
                continue
            endpoint = getattr(route, "endpoint", None)
            meta = getattr(endpoint, "__shield_meta__", {}) if endpoint else {}
            is_force_active = bool(meta.get("force_active"))
            response_factory = meta.get("response_factory")
            template = getattr(route, "path", None) or ""

            if "{" not in template:
                # Static path — exact dict key match on every request.
                static[template] = (is_force_active, template, response_factory)
            else:
                # Parameterised path — requires regex matching per request
                # but the list is usually a small fraction of total routes.
                param.append((route, is_force_active, template, response_factory))

        self._static_route_meta = static
        self._param_routes = param
        self._route_cache_built = True

    async def _ensure_routes_scanned(self, app: Any) -> None:
        """Lazy fallback scan for environments without ASGI lifespan support."""
        if self._routes_scanned:
            return
        await self._do_scan(app)

    # ------------------------------------------------------------------
    # HTTP dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Intercept the request and run shield checks."""
        path = request.url.path

        # Run the lazy scan on EVERY request (including /docs, /openapi.json)
        # so OpenAPI filtering works even if the ASGI lifespan was not used.
        # After the lifespan scan, this is a no-op (flag already set).
        await self._ensure_routes_scanned(request.app)

        # Always skip internal/docs prefixes for shield enforcement.
        if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return await call_next(request)

        # Single route scan: resolves force_active flag, the template path,
        # and the optional custom response factory in one pass.
        is_force_active, template_path, response_factory = self._resolve_route(request)

        # Use the route template for engine lookups so parameterised routes
        # (e.g. /items/{item_id}) match their registered state key correctly.
        # Fall back to the concrete URL for unmatched paths.
        check_path = template_path or path

        if is_force_active:
            # @force_active routes bypass all shield checks by default.
            # Exception: when global maintenance is enabled AND configured to
            # include force-active routes, we fall through to engine.check().
            try:
                global_cfg = await self.engine.get_global_maintenance()
                if not (global_cfg.enabled and global_cfg.include_force_active):
                    return await call_next(request)
                # Global maintenance overrides @force_active — fall through.
            except Exception:
                # Fail-open: backend unreachable → honour force_active.
                return await call_next(request)

        try:
            await self.engine.check(check_path, method=request.method)
        except MaintenanceException as exc:
            factory = response_factory or self._default_responses.get("maintenance")
            if factory:
                return await self._call_response_factory(factory, request, exc)
            return self._maintenance_response(path, exc)
        except RouteDisabledException as exc:
            factory = response_factory or self._default_responses.get("disabled")
            if factory:
                return await self._call_response_factory(factory, request, exc)
            return self._disabled_response(path, exc)
        except EnvGatedException as exc:
            factory = response_factory or self._default_responses.get("env_gated")
            if factory:
                return await self._call_response_factory(factory, request, exc)
            # Silent 404 — do not reveal that the route exists.
            return Response(status_code=404)

        response = await call_next(request)

        # Inject deprecation headers for DEPRECATED routes (does not block).
        response = await self._inject_deprecation_headers(check_path, request.method, response)
        return response

    def _resolve_route(self, request: Request) -> tuple[bool, str | None, Any]:
        """Match the request against app routes using the pre-built cache.

        Returns ``(is_force_active, template_path, response_factory)`` where:

        - ``is_force_active`` is ``True`` when the matched endpoint carries
          ``@force_active`` and should bypass all shield checks.
        - ``template_path`` is the route's path *template* (e.g.
          ``"/items/{item_id}"``), which is what the engine stores as its
          state key.  Using the template instead of the concrete URL means
          that parameterised routes (``/items/42`` → ``/items/{item_id}``)
          are resolved correctly.
        - ``response_factory`` is the callable stamped by ``@shield_response``,
          or ``None`` when no custom response has been declared.

        Returns ``(False, None, None)`` when no route matches (unregistered path).

        Performance
        -----------
        After ``_build_route_cache()`` runs at startup:

        * Static paths (no ``{params}``) resolve in O(1) via dict lookup.
        * Parameterised paths scan only ``_param_routes`` — a small subset of
          all routes — rather than iterating the entire route list on every
          request.

        Falls back to the original O(N) walk when the cache is not yet built
        (e.g. in environments without ASGI lifespan support where the lazy
        scan has not completed for the current request).
        """
        path = request.url.path

        if self._route_cache_built:
            # Fast path: O(1) dict lookup for static routes.
            entry = self._static_route_meta.get(path)
            if entry is not None:
                return entry

            # Parameterised routes — scan only the short param-route list.
            for route, is_force_active, template, response_factory in self._param_routes:
                match, _ = route.matches(request.scope)
                if match == Match.FULL:
                    return is_force_active, template, response_factory

            return False, None, None

        # Fallback: full O(N) walk used before the cache is ready.
        routes = getattr(request.app, "routes", [])
        for route in routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                endpoint = getattr(route, "endpoint", None)
                meta = getattr(endpoint, "__shield_meta__", {}) if endpoint else {}
                return (
                    bool(meta.get("force_active")),
                    getattr(route, "path", None),
                    meta.get("response_factory"),
                )
        return False, None, None

    async def _inject_deprecation_headers(
        self, path: str, method: str, response: Response
    ) -> Response:
        """Add Deprecation/Sunset/Link headers if the route is DEPRECATED.

        Uses the same method-aware resolution as ``engine.check()``:
        checks ``METHOD:/path`` first, then falls back to ``/path``.
        """
        state = await self.engine._resolve_state(path, method)
        if state is None:
            return response

        if state.status != RouteStatus.DEPRECATED:
            return response

        # MutableHeaders lets us add headers to the already-built response.
        response.headers["Deprecation"] = "true"
        if state.sunset_date:
            response.headers["Sunset"] = state.sunset_date
        if state.successor_path:
            response.headers["Link"] = f'<{state.successor_path}>; rel="successor-version"'
        return response

    # ------------------------------------------------------------------
    # Response builders
    # ------------------------------------------------------------------

    @staticmethod
    async def _call_response_factory(factory: Any, request: Request, exc: Exception) -> Response:
        """Invoke a user-supplied response factory (sync or async).

        The factory receives the live ``Request`` and the ``ShieldException``
        that triggered the block, and must return a Starlette ``Response``.
        Both sync and async factories are supported.
        """
        result: Any = factory(request, exc)
        if asyncio.iscoroutine(result):
            return cast(Response, await result)
        return cast(Response, result)

    @staticmethod
    def _maintenance_response(path: str, exc: MaintenanceException) -> JSONResponse:
        retry_after = exc.retry_after.isoformat() if exc.retry_after else None
        body: dict[str, Any] = {
            "error": {
                "code": "MAINTENANCE_MODE",
                "message": "This endpoint is temporarily unavailable",
                "reason": exc.reason,
                "path": path,
            }
        }
        if retry_after:
            body["error"]["retry_after"] = retry_after

        headers = {}
        if retry_after:
            headers["Retry-After"] = retry_after

        return JSONResponse(status_code=503, content=body, headers=headers)

    @staticmethod
    def _disabled_response(path: str, exc: RouteDisabledException) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "ROUTE_DISABLED",
                    "message": "This endpoint has been disabled",
                    "reason": exc.reason,
                    "path": path,
                }
            },
        )
