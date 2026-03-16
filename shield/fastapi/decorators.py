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
from starlette.responses import Response

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

# Signature for deps that also need to write response headers (e.g. @deprecated).
# FastAPI injects both the incoming Request and the mutable Response object.
_REQUEST_RESPONSE_SIGNATURE = inspect.Signature(
    [
        inspect.Parameter(
            "request",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Request,
        ),
        inspect.Parameter(
            "response",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Response,
        ),
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
        dep_raise: Callable[..., None] | None = None,
        signature: inspect.Signature | None = None,
    ) -> None:
        self._meta = meta
        self._dep_raise = dep_raise
        # FastAPI calls inspect.signature(dep) to resolve DI parameters.
        # Setting __signature__ on the instance is the canonical way to
        # override this without subclassing.
        self.__signature__ = signature or _REQUEST_SIGNATURE

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
                response = kwargs.get("response")
                if response is not None:
                    self._dep_raise(func_or_request, response)
                else:
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
) -> _ShieldCallable:
    """Mark a route as deprecated.

    The route still serves requests but:

    - The middleware injects ``Deprecation: true``, ``Sunset``, and
      optionally ``Link`` response headers on every response.
    - The OpenAPI schema marks the route as ``deprecated: true``.
    - ``ShieldRouter`` registers the route with status ``DEPRECATED``.

    Also works as a ``Depends()`` dependency — when used without middleware,
    the dep injects the same RFC-compliant headers directly on the response.

    Parameters
    ----------
    sunset:
        RFC 7231 date string indicating when the route will be removed
        (e.g. ``"Sat, 01 Jan 2026 00:00:00 GMT"``).  Also accepted as an
        ISO-8601 datetime string for convenience.
    use_instead:
        Path of the successor route.  Injected as a
        ``Link: <path>; rel="successor-version"`` header when set.

    Examples
    --------
    Decorator (middleware handles header injection automatically)::

        @router.get("/v1/users")
        @deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/users")
        async def v1_users(): ...

    Dependency (injects headers even without middleware)::

        @router.get(
            "/v1/users",
            dependencies=[Depends(deprecated(
                sunset="Sat, 01 Jan 2027 00:00:00 GMT",
                use_instead="/v2/users",
            ))],
        )
        async def v1_users(): ...
    """
    meta: dict[str, Any] = {
        "status": "deprecated",
        "sunset_date": sunset,
        "successor_path": use_instead or None,
    }

    def dep_raise(request: Request, response: Response) -> None:  # noqa: ARG001
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = sunset
        if use_instead:
            response.headers["Link"] = f'<{use_instead}>; rel="successor-version"'

    return _ShieldCallable(
        meta=meta,
        dep_raise=dep_raise,
        signature=_REQUEST_RESPONSE_SIGNATURE,
    )


def rate_limit(
    limit: str | dict[str, str],
    *,
    algorithm: Any = None,
    key: Any = None,
    on_missing_key: Any = None,
    burst: int = 0,
    exempt_ips: list[str] | None = None,
    exempt_roles: list[str] | None = None,
    tier_resolver: str = "plan",
    response: ResponseFactory | None = None,
) -> _ShieldCallable:
    """Apply a rate limit to a route.

    Works as both a decorator (stamps ``__shield_meta__``) and a FastAPI
    dependency (enforces the limit at request time when the engine is
    available via ``configure_shield``).

    Parameters
    ----------
    limit:
        Rate limit string in ``limits`` format, e.g. ``"100/minute"``, or a
        dict mapping tier names to limits: ``{"free": "100/day", "pro": "10000/day"}``.
    algorithm:
        ``RateLimitAlgorithm`` enum value.  Defaults to ``FIXED_WINDOW``.
    key:
        ``RateLimitKeyStrategy`` enum value **or** an async callable
        ``(Request) -> str | None`` for custom key extraction.
        Defaults to ``RateLimitKeyStrategy.IP``.
    on_missing_key:
        ``OnMissingKey`` enum value.  When ``None``, the per-strategy
        default is applied (see ``RateLimitKeyStrategy`` docstrings).
    burst:
        Extra requests allowed above the base limit.
    exempt_ips:
        List of IP addresses / CIDR networks exempt from this limit.
    exempt_roles:
        List of roles exempt from this limit (checked against
        ``request.state.user_roles``).
    tier_resolver:
        Name of the ``request.state`` attribute used to look up the
        caller's tier when *limit* is a dict.  Defaults to ``"plan"``.
    response:
        Optional custom response factory for rate limit violations — a
        sync or async callable with signature
        ``(request: Request, exc: Exception) -> Response``.
        When provided, this factory is called instead of the default
        429 JSON body.  Falls back to the middleware
        ``responses["rate_limited"]`` global default if not set here.

    Examples
    --------
    IP-based limit (safe default, never missing)::

        @router.get("/search")
        @rate_limit("100/minute")
        async def search(): ...

    Per-user limit (requires auth middleware to set ``request.state.user_id``)::

        @router.get("/export")
        @rate_limit("10/hour", key=RateLimitKeyStrategy.USER)
        async def export(): ...

    Tiered limits::

        @router.get("/api/data")
        @rate_limit({"free": "100/day", "pro": "10000/day", "enterprise": "unlimited"})
        async def get_data(): ...

    Custom key extractor::

        async def by_org(request: Request) -> str | None:
            return getattr(request.state, "org_id", None)

        @router.get("/api/bulk")
        @rate_limit("1000/hour", key=by_org)
        async def bulk(): ...
    """
    # Resolve lazy imports to avoid circular imports at module load time.
    from shield.core.rate_limit.models import (
        RateLimitAlgorithm,
        RateLimitKeyStrategy,
        RateLimitTier,
    )

    # Default algorithm.
    if algorithm is None:
        algorithm = RateLimitAlgorithm.FIXED_WINDOW

    # Resolve key strategy and optional custom callable.
    custom_key_func: Any = None
    if key is None:
        resolved_strategy = RateLimitKeyStrategy.IP
    elif callable(key) and not isinstance(key, RateLimitKeyStrategy):
        resolved_strategy = RateLimitKeyStrategy.CUSTOM
        custom_key_func = key
    else:
        resolved_strategy = RateLimitKeyStrategy(key)

    # Normalise tiered limits.
    tiers: list[RateLimitTier] = []
    if isinstance(limit, dict):
        tiers = [RateLimitTier(name=k, limit=v) for k, v in limit.items()]
        limit_str = list(limit.values())[0]  # fallback limit = first tier value
        # Tiered limits imply per-user limiting unless explicitly overridden.
        if key is None:
            resolved_strategy = RateLimitKeyStrategy.USER
    else:
        limit_str = limit

    rate_limit_meta: dict[str, Any] = {
        "limit": limit_str,
        "algorithm": algorithm,
        "key_strategy": resolved_strategy,
        "on_missing_key": on_missing_key,
        "burst": burst,
        "tiers": [t.model_dump() for t in tiers],
        "tier_resolver": tier_resolver,
        "exempt_ips": exempt_ips or [],
        "exempt_roles": exempt_roles or [],
    }
    if custom_key_func is not None:
        rate_limit_meta["key_func"] = custom_key_func

    meta: dict[str, Any] = {
        "rate_limit": rate_limit_meta,
        "rate_limit_response_factory": response,
    }

    def dep_raise(request: Request) -> None:
        """Enforce rate limit as a FastAPI dependency."""
        eff_engine = _resolve_engine(None, request)
        if eff_engine is None:
            return  # no engine — fail-open; rely on middleware

        path = request.url.path
        method = request.method
        policy_key = f"{method.upper()}:{path}"
        if eff_engine._rate_limiter is None:
            return
        policy = eff_engine._rate_limit_policies.get(policy_key)
        if policy is None:
            return
        # Run the async check from a sync dep thread.
        import anyio.from_thread as _aft

        try:
            result = _aft.run(
                eff_engine._rate_limiter.check,
                path,
                method,
                request,
                policy,
                custom_key_func,
            )
        except Exception:
            return  # fail-open on any error

        if not result.allowed:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Too many requests",
                    "limit": result.limit,
                    "retry_after_seconds": result.retry_after_seconds,
                    "reset_at": result.reset_at.isoformat(),
                    "path": path,
                },
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "X-RateLimit-Limit": result.limit,
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(result.reset_at.timestamp())),
                },
            )

    return _ShieldCallable(meta=meta, dep_raise=dep_raise)


def force_active(func: F) -> F:
    """Force a route to bypass all shield checks.

    Use this for routes that must always be reachable, such as health checks
    and status endpoints.

    Note
    ----
    ``@force_active`` is a decorator-only construct and cannot be used as a
    ``Depends()`` dependency.  Shield checks are enforced by the middleware,
    which runs *before* any dependency is resolved.  A dependency has no way
    to signal to the already-completed middleware that it should have skipped
    the check.  If you need a route to always be reachable, apply
    ``@force_active`` as a decorator — that is the only place where it takes
    effect.
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
