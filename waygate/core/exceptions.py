"""Exceptions raised by the waygate engine during route lifecycle checks."""

from __future__ import annotations

import warnings
from datetime import datetime


class WaygateException(Exception):
    """Base exception for all waygate errors."""


class MaintenanceException(WaygateException):
    """Raised when a route is in maintenance mode."""

    def __init__(self, reason: str = "", retry_after: datetime | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


class EnvGatedException(WaygateException):
    """Raised when a route is restricted to specific environments."""

    def __init__(self, path: str, current_env: str, allowed_envs: list[str]) -> None:
        self.path = path
        self.current_env = current_env
        self.allowed_envs = allowed_envs
        super().__init__(
            f"Route {path!r} is not available in environment {current_env!r}. "
            f"Allowed: {allowed_envs}"
        )


class RouteDisabledException(WaygateException):
    """Raised when a route has been permanently disabled."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(reason)


class RouteNotFoundException(WaygateException):
    """Raised when a route key is not registered in the backend.

    Mutation operations (enable, disable, set_maintenance, …) refuse to
    create new state entries for unknown routes.  Use the route key exactly
    as it appears in ``waygate status`` output (e.g. ``GET:/payments``).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"Route {path!r} is not registered. Use 'waygate status' to see all registered routes."
        )


class AmbiguousRouteError(WaygateException):
    """Raised when a bare path matches more than one method-prefixed route.

    For example, ``/payments`` is ambiguous when both ``GET:/payments`` and
    ``POST:/payments`` are registered.  Specify the full key
    (e.g. ``GET:/payments``) or confirm in the CLI to apply to all matches.
    """

    def __init__(self, path: str, matches: list[str]) -> None:
        self.path = path
        self.matches = matches
        super().__init__(
            f"Route {path!r} is ambiguous — matches: {', '.join(matches)}. "
            "Specify the full method-prefixed key (e.g. GET:/payments)."
        )


class RouteProtectedException(WaygateException):
    """Raised when attempting to mutate a ``@force_active`` route.

    Routes decorated with ``@force_active`` are permanently locked to the
    ACTIVE state.  Their status cannot be changed via the engine, CLI, or
    dashboard — this is by design so that critical routes (health checks,
    status endpoints) can never be accidentally taken down.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"Route {path!r} is decorated with @force_active and cannot "
            "have its state changed. Remove the decorator first if you need "
            "to control this route's lifecycle."
        )


class RateLimitExceededException(WaygateException):
    """Raised when a request exceeds the configured rate limit for a route.

    Carries all metadata needed to build the 429 response and set the
    RFC-compliant ``Retry-After``, ``X-RateLimit-*`` response headers.
    """

    def __init__(
        self,
        limit: str,
        retry_after_seconds: int,
        reset_at: datetime,
        remaining: int,
        key: str,
    ) -> None:
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at
        self.remaining = remaining
        self.key = key
        super().__init__(f"Rate limit exceeded: {limit}")


class WaygateProductionWarning(UserWarning):
    """Emitted when a configuration is valid but has known limitations in
    production multi-worker deployments.

    The feature will work correctly for single-process deployments.

    Suppress with ``warnings.filterwarnings("ignore",
    category=WaygateProductionWarning)`` if you understand and accept the
    limitation.
    """

    pass


# Keep ``warnings`` importable without side effects — just expose the symbol.
__all__ = [
    "WaygateException",
    "MaintenanceException",
    "EnvGatedException",
    "RouteDisabledException",
    "RouteNotFoundException",
    "AmbiguousRouteError",
    "RouteProtectedException",
    "RateLimitExceededException",
    "WaygateProductionWarning",
]

# Silence "imported but unused" linter warning for the ``warnings`` stdlib
# module that is imported at the top of this file.
_ = warnings
