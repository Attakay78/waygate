"""FastAPI route decorators for api-shield.

Decorators are metadata stamps — they attach ``__shield_meta__`` to the
route function. ``ShieldRouter`` reads this metadata at startup and registers
the appropriate state via ``ShieldEngine``. The decorators themselves do
**no** request-time logic; the middleware handles everything.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def _stamp(func: F, meta: dict[str, Any]) -> F:
    """Attach *meta* as ``__shield_meta__`` on *func*, merging if already set."""
    existing: dict[str, Any] = getattr(func, "__shield_meta__", {})
    existing.update(meta)
    func.__shield_meta__ = existing  # type: ignore[attr-defined]
    return func


def maintenance(
    reason: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
) -> Callable[[F], F]:
    """Mark a route as being in maintenance mode.

    Parameters
    ----------
    reason:
        Human-readable explanation shown in the 503 response body.
    start:
        Optional datetime when maintenance begins (for scheduled windows).
    end:
        Optional datetime when maintenance ends (sets ``Retry-After`` header).
    """
    from shield.core.models import MaintenanceWindow

    window = MaintenanceWindow(start=start, end=end) if start and end else None

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper = cast(F, async_wrapper if _is_async(func) else sync_wrapper)
        return _stamp(wrapper, {"status": "maintenance", "reason": reason, "window": window})

    return decorator


def env_only(*envs: str) -> Callable[[F], F]:
    """Restrict a route to the given environment names.

    In any other environment the middleware returns a silent 404.

    Parameters
    ----------
    envs:
        One or more environment names (e.g. ``"dev"``, ``"staging"``).
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper = cast(F, async_wrapper if _is_async(func) else sync_wrapper)
        return _stamp(wrapper, {"status": "env_gated", "allowed_envs": list(envs)})

    return decorator


def disabled(reason: str = "") -> Callable[[F], F]:
    """Permanently disable a route (returns 503).

    Parameters
    ----------
    reason:
        Human-readable explanation shown in the 503 response body.
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper = cast(F, async_wrapper if _is_async(func) else sync_wrapper)
        return _stamp(wrapper, {"status": "disabled", "reason": reason})

    return decorator


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
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper = cast(F, async_wrapper if _is_async(func) else sync_wrapper)
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
    """

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    wrapper = cast(F, async_wrapper if _is_async(func) else sync_wrapper)
    return _stamp(wrapper, {"force_active": True})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_async(func: Callable[..., Any]) -> bool:
    """Return True if *func* is a coroutine function."""
    import asyncio

    return asyncio.iscoroutinefunction(func)
