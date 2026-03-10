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
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match
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
    """

    def __init__(self, app: ASGIApp, engine: ShieldEngine) -> None:
        super().__init__(app)
        self.engine = engine
        self._scan_lock: asyncio.Lock = asyncio.Lock()
        self._routes_scanned: bool = False

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

        # Single route scan: resolves force_active flag AND the template path
        # in one pass instead of two separate walks through app.routes.
        is_force_active, template_path = self._resolve_route(request)

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
            return self._maintenance_response(path, exc)
        except RouteDisabledException as exc:
            return self._disabled_response(path, exc)
        except EnvGatedException:
            # Silent 404 — do not reveal that the route exists.
            return Response(status_code=404)

        response = await call_next(request)

        # Inject deprecation headers for DEPRECATED routes (does not block).
        response = await self._inject_deprecation_headers(check_path, request.method, response)
        return response

    def _resolve_route(self, request: Request) -> tuple[bool, str | None]:
        """Match the request against app routes in a single pass.

        Returns ``(is_force_active, template_path)`` where:

        - ``is_force_active`` is ``True`` when the matched endpoint carries
          ``@force_active`` and should bypass all shield checks.
        - ``template_path`` is the route's path *template* (e.g.
          ``"/items/{item_id}"``), which is what the engine stores as its
          state key.  Using the template instead of the concrete URL means
          that parameterised routes (``/items/42`` → ``/items/{item_id}``)
          are resolved correctly.

        Returns ``(False, None)`` when no route matches (unregistered path).
        """
        routes = getattr(request.app, "routes", [])
        for route in routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                endpoint = getattr(route, "endpoint", None)
                meta = getattr(endpoint, "__shield_meta__", {}) if endpoint else {}
                return bool(meta.get("force_active")), getattr(route, "path", None)
        return False, None

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
