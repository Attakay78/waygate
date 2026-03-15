"""FastAPI route decorators for api-shield.

Every decorator returns a :class:`_ShieldCallable` — a dual-purpose object:

* **As a decorator**: stamps ``__shield_meta__`` on the route function.
  ``ShieldRouter`` reads this at startup and registers the initial state in
  the backend.  The middleware then enforces it on every request.

* **As a FastAPI dependency** (via ``Depends()``): enforces the route state
  at request time.  Three modes depending on how the engine is supplied:

  1. **Engine from app state** (recommended — zero config per route):
     Call ``configure_shield(app, engine)`` once (or use ``ShieldMiddleware``
     which does this automatically).  All decorator deps then find the engine
     via ``request.app.state.shield_engine`` without any ``engine=`` argument.

  2. **Explicit engine** (``engine=engine``): overrides the app-state lookup.
     Useful when you have multiple engines or don't use ``configure_shield``.

  3. **Inline / stateless** (no engine anywhere): ``maintenance`` and
     ``disabled`` always raise the declared error.  ``env_only`` fails open
     (use middleware for the enforcement).  No backend, no runtime toggling.

All three styles use the same import — no ``_dep`` suffix, no separate class::

    from shield.fastapi import maintenance, disabled, env_only, configure_shield

    # Once, in app setup:
    configure_shield(app, engine)  # or: app.add_middleware(ShieldMiddleware, engine=engine)

    # Decorator (state registered in backend, togglable at runtime via CLI/dashboard):
    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments(): ...

    # Dependency — engine resolved from app state automatically:
    @router.get("/payments", dependencies=[Depends(maintenance(reason="DB migration"))])
    async def get_payments(): ...

    # Explicit engine override (bypasses app state lookup):
    @router.get(
        "/payments",
        dependencies=[Depends(maintenance(reason="DB migration", engine=engine))],
    )
    async def get_payments(): ...

Note: for engine-backed deps to work correctly, the route state must be
registered in the backend (either via ``ShieldRouter`` at startup, or by
calling ``engine.set_maintenance()`` / ``engine.disable()`` manually).
Unregistered paths are ACTIVE by default (fail-open behaviour is preserved).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, cast

import anyio.from_thread
from fastapi import HTTPException
from starlette.requests import Request

from shield.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from shield.core.models import MaintenanceWindow

F = TypeVar("F", bound=Callable[..., Any])

# A response factory is any callable that receives the live Request and the
# ShieldException that triggered the block, and returns a Starlette Response.
# Both sync and async callables are supported.
#
# Example::
#
#     from starlette.responses import HTMLResponse
#
#     def my_factory(request: Request, exc: Exception) -> HTMLResponse:
#         return HTMLResponse("<h1>Down for maintenance</h1>", status_code=503)
#
ResponseFactory = Callable[..., Any]

# Pre-built signature that FastAPI's DI system uses to inject a Request.
# inspect.signature() checks __signature__ on the instance before anything else.
_REQUEST_SIGNATURE = inspect.Signature(
    [
        inspect.Parameter(
            "request",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Request,
        )
    ]
)

_SHIELD_ENGINE_ATTR = "shield_engine"


def _format_retry_after(dt: datetime | None) -> str | None:
    """Convert datetime to ISO format string for Retry-After header, or None."""
    return dt.isoformat() if dt else None


def _build_maintenance_exception(
    path: str, reason: str, retry_after: str | None = None
) -> HTTPException:
    """Build an HTTPException for a route in maintenance mode."""
    detail: dict[str, Any] = {
        "code": "MAINTENANCE_MODE",
        "message": "This endpoint is temporarily unavailable",
        "reason": reason,
        "path": path,
    }
    if retry_after:
        detail["retry_after"] = retry_after
    headers = {"Retry-After": retry_after} if retry_after else {}
    return HTTPException(status_code=503, detail=detail, headers=headers)


def _build_disabled_exception(path: str, reason: str) -> HTTPException:
    """Build an HTTPException for a disabled route."""
    return HTTPException(
        status_code=503,
        detail={
            "code": "ROUTE_DISABLED",
            "message": "This endpoint has been disabled",
            "reason": reason,
            "path": path,
        },
    )


def _resolve_engine(explicit: Any, request: Request) -> Any:
    """Return the effective engine for a dep call.

    Priority:
    1. *explicit* — passed as ``engine=`` at decoration time.
    2. ``request.app.state.shield_engine`` — set by ``configure_shield`` or
       ``ShieldMiddleware`` at ASGI lifespan startup.
    3. ``None`` — no engine available; caller falls back to inline logic.
    """
    if explicit is not None:
        return explicit
    return getattr(request.app.state, _SHIELD_ENGINE_ATTR, None)


def _engine_dep_raise(engine: Any, request: Request) -> None:
    """Call ``engine.check()`` from a FastAPI sync dependency thread.

    FastAPI runs sync dependencies in anyio worker threads
    (``anyio.to_thread.run_sync``).  ``anyio.from_thread.run`` is the
    matching API for calling an async function back on the event loop from
    within such a thread.  ShieldExceptions are caught here and re-raised
    as ``HTTPException``.
    """
    path = request.url.path
    method = request.method
    try:
        anyio.from_thread.run(engine.check, path, method)
    except MaintenanceException as exc:
        retry_after = _format_retry_after(exc.retry_after)
        raise _build_maintenance_exception(path, exc.reason, retry_after)
    except RouteDisabledException as exc:
        raise _build_disabled_exception(path, exc.reason)
    except EnvGatedException:
        raise HTTPException(status_code=404)


def _stamp(func: F, meta: dict[str, Any]) -> F:
    """Attach *meta* as ``__shield_meta__`` on *func*, merging if already set."""
    existing: dict[str, Any] = getattr(func, "__shield_meta__", {})
    existing.update(meta)
    func.__shield_meta__ = existing  # type: ignore[attr-defined]
    return func


def _make_wrapper(func: F) -> F:
    """Return a thin async or sync wrapper around *func* that preserves metadata."""

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return cast(F, async_wrapper if _is_async(func) else sync_wrapper)


class _ShieldCallable:
    """Dual-purpose callable: decorator or FastAPI dependency.

    When called with a **function** (decorator use), stamps ``__shield_meta__``
    on the wrapped function and returns it.

    When called with a **Request** (FastAPI dependency use), invokes
    ``dep_raise`` which raises the appropriate ``HTTPException``.  FastAPI
    discovers the ``(request: Request)`` signature via ``__signature__`` and
    injects the live request automatically.
    """

    def __init__(
        self,
        meta: dict[str, Any],
        dep_raise: Callable[[Request], None] | None = None,
    ) -> None:
        self._meta = meta
        self._dep_raise = dep_raise
        # FastAPI calls inspect.signature(dep) to resolve DI parameters.
        # Setting __signature__ on the instance is the canonical way to
        # override this without subclassing.
        self.__signature__ = _REQUEST_SIGNATURE

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Dispatch to decorator path or dependency path based on argument type.

        FastAPI calls dependencies by keyword using the parameter name from
        ``__signature__``, so the live ``Request`` arrives as
        ``kwargs["request"]``.  Python's decorator protocol calls with a
        positional argument, so the decorated function arrives in ``args[0]``.
        """
        func_or_request = args[0] if args else kwargs.get("request")

        if isinstance(func_or_request, Request):
            # FastAPI dependency path — called by the DI system with the
            # live request.  Raise HTTPException to block; return None to
            # allow (fail-open when dep_raise is absent).
            if self._dep_raise is not None:
                self._dep_raise(func_or_request)
            return None

        # Decorator path — func_or_request is the function being decorated.
        if not callable(func_or_request):
            raise TypeError(f"Expected a callable, got {type(func_or_request)!r}")
        return _stamp(_make_wrapper(func_or_request), self._meta)


# ---------------------------------------------------------------------------
# Public decorators
# ---------------------------------------------------------------------------


def maintenance(
    reason: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    engine: Any = None,
    response: ResponseFactory | None = None,
) -> _ShieldCallable:
    """Mark a route as being in maintenance mode.

    Parameters
    ----------
    reason:
        Human-readable explanation shown in the 503 response body.
    start:
        Optional datetime when maintenance begins.  When provided together
        with *end*, the inline dep respects the window; requests outside the
        window pass through.  Ignored when an engine is available (the
        engine's scheduled window takes precedence).
    end:
        Optional datetime when maintenance ends.  Sets ``Retry-After`` header.
    engine:
        Optional explicit engine override.  When omitted, the dep looks up
        ``app.state.shield_engine`` (set by ``configure_shield`` or
        ``ShieldMiddleware``).  Pass explicitly only when you need to target a
        specific engine that differs from the app-level one.
    response:
        Optional custom response factory — a sync or async callable with
        signature ``(request: Request, exc: Exception) -> Response``.
        When provided, this factory is called instead of the default JSON
        error body.  Accepts any Starlette ``Response`` subclass (HTML,
        redirect, plain text, custom JSON, …).  Falls back to the middleware
        ``responses["maintenance"]`` global default if not set here.

    Examples
    --------
    Default JSON response::

        @router.get("/payments")
        @maintenance(reason="DB migration")
        async def payments(): ...

    Inline HTML response::

        from starlette.responses import HTMLResponse

        @router.get("/payments")
        @maintenance(reason="DB migration", response=lambda req, exc: HTMLResponse(
            f"<h1>Down for maintenance</h1><p>{exc.reason}</p>", status_code=503
        ))
        async def payments(): ...

    Shared factory across routes::

        def maintenance_page(request, exc):
            return HTMLResponse("<h1>Back soon</h1>", status_code=503)

        @router.get("/payments")
        @maintenance(reason="DB migration", response=maintenance_page)
        async def payments(): ...

    Zero-config dep (engine resolved from app state automatically)::

        configure_shield(app, engine)  # once

        @router.get("/payments", dependencies=[Depends(maintenance(reason="Migration"))])
        async def payments(): ...
    """
    window = MaintenanceWindow(start=start, end=end) if start and end else None
    meta: dict[str, Any] = {
        "status": "maintenance",
        "reason": reason,
        "window": window,
        "response_factory": response,
    }

    def dep_raise(request: Request) -> None:
        eff_engine = _resolve_engine(engine, request)
        if eff_engine is not None:
            _engine_dep_raise(eff_engine, request)
            return
        # No engine: inline time-window check then always block.
        if start is not None and end is not None:
            now = datetime.now(UTC)
            if now < start or now > end:
                return  # outside declared window — pass through
        retry_after = _format_retry_after(end)
        raise _build_maintenance_exception(request.url.path, reason, retry_after)

    return _ShieldCallable(meta=meta, dep_raise=dep_raise)


def env_only(
    *envs: str, engine: Any = None, response: ResponseFactory | None = None
) -> _ShieldCallable:
    """Restrict a route to the given environment names.

    In any other environment returns a silent 404 by default.

    Parameters
    ----------
    envs:
        One or more environment names (e.g. ``"dev"``, ``"staging"``).
    engine:
        Optional explicit engine override.  When omitted, the dep looks up
        ``app.state.shield_engine``.  If no engine is found anywhere, the dep
        fails open (use ``ShieldMiddleware`` for middleware-level enforcement).
    response:
        Optional custom response factory — a sync or async callable with
        signature ``(request: Request, exc: Exception) -> Response``.
        When provided, this factory is called instead of the default silent
        404.  Falls back to the middleware ``responses["env_gated"]`` global
        default if not set here.

    Examples
    --------
    Zero-config dep::

        configure_shield(app, engine)  # once

        @router.get("/debug", dependencies=[Depends(env_only("dev", "staging"))])
        async def debug(): ...

    Custom response for wrong environment::

        @router.get("/internal")
        @env_only("dev", "staging", response=lambda req, exc: PlainTextResponse(
            "Not available in this environment.", status_code=404
        ))
        async def internal(): ...
    """
    meta: dict[str, Any] = {
        "status": "env_gated",
        "allowed_envs": list(envs),
        "response_factory": response,
    }

    def dep_raise(_request: Request) -> None:
        eff_engine = _resolve_engine(engine, _request)
        if eff_engine is None:
            return  # no engine — fail-open; rely on middleware
        if eff_engine.current_env not in envs:
            raise HTTPException(status_code=404)

    return _ShieldCallable(meta=meta, dep_raise=dep_raise)


def disabled(
    reason: str = "", engine: Any = None, response: ResponseFactory | None = None
) -> _ShieldCallable:
    """Permanently disable a route (returns 503).

    Parameters
    ----------
    reason:
        Human-readable explanation shown in the 503 response body.
    engine:
        Optional explicit engine override.  When omitted, the dep looks up
        ``app.state.shield_engine``.  When an engine is available the route
        can be re-enabled at runtime via ``shield enable <path>`` or the
        dashboard.
    response:
        Optional custom response factory — a sync or async callable with
        signature ``(request: Request, exc: Exception) -> Response``.
        When provided, this factory is called instead of the default JSON
        503 body.  Falls back to the middleware ``responses["disabled"]``
        global default if not set here.

    Examples
    --------
    Zero-config dep (re-enable with ``shield enable /old-endpoint``)::

        configure_shield(app, engine)  # once

        @router.get("/old-endpoint", dependencies=[Depends(disabled(reason="Use /v2"))])
        async def old_endpoint(): ...

    Custom response::

        @router.get("/legacy")
        @disabled(reason="Retired. Use /v2/orders.", response=lambda req, exc:
            PlainTextResponse(f"Gone. {exc.reason}", status_code=503))
        async def legacy(): ...
    """
    meta: dict[str, Any] = {
        "status": "disabled",
        "reason": reason,
        "response_factory": response,
    }

    def dep_raise(request: Request) -> None:
        eff_engine = _resolve_engine(engine, request)
        if eff_engine is not None:
            _engine_dep_raise(eff_engine, request)
            return
        # No engine: always block.
        raise _build_disabled_exception(request.url.path, reason)

    return _ShieldCallable(meta=meta, dep_raise=dep_raise)


def deprecated(
    sunset: str,
    use_instead: str = "",
) -> Callable[[F], F]:
    """Mark a route as deprecated.

    The route still serves requests but:

    - The middleware injects ``Deprecation: true``, ``Sunset``, and
      optionally ``Link`` response headers on every response.
    - The OpenAPI schema marks the route as ``deprecated: true``.
    - ``ShieldRouter`` registers the route with status ``DEPRECATED``.

    Parameters
    ----------
    sunset:
        RFC 7231 date string indicating when the route will be removed
        (e.g. ``"Sat, 01 Jan 2026 00:00:00 GMT"``).  Also accepted as an
        ISO-8601 datetime string for convenience.
    use_instead:
        Path of the successor route.  Injected as a
        ``Link: <path>; rel="successor-version"`` header when set.

    Note
    ----
    ``@deprecated`` is a decorator-only construct.  It does not block
    requests — it injects headers.  Header injection is handled by the
    middleware, so there is no dependency variant.
    """

    def decorator(func: F) -> F:
        wrapper = _make_wrapper(func)
        return _stamp(
            wrapper,
            {
                "status": "deprecated",
                "sunset_date": sunset,
                "successor_path": use_instead or None,
            },
        )

    return decorator


def force_active(func: F) -> F:
    """Force a route to bypass all shield checks.

    Use this for routes that must always be reachable, such as health checks
    and status endpoints.

    Note
    ----
    ``@force_active`` is a decorator-only construct.  Using it as a FastAPI
    dependency would be a no-op (routes are ACTIVE by default).
    """
    wrapper = _make_wrapper(func)
    return _stamp(wrapper, {"force_active": True})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_async(func: Callable[..., Any]) -> bool:
    """Return True if *func* is a coroutine function."""
    import asyncio

    return asyncio.iscoroutinefunction(func)
