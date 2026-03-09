"""Exceptions raised by the shield engine during route lifecycle checks."""

from __future__ import annotations

from datetime import datetime


class ShieldException(Exception):
    """Base exception for all api-shield errors."""


class MaintenanceException(ShieldException):
    """Raised when a route is in maintenance mode."""

    def __init__(self, reason: str = "", retry_after: datetime | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


class EnvGatedException(ShieldException):
    """Raised when a route is restricted to specific environments."""

    def __init__(self, path: str, current_env: str, allowed_envs: list[str]) -> None:
        self.path = path
        self.current_env = current_env
        self.allowed_envs = allowed_envs
        super().__init__(
            f"Route {path!r} is not available in environment {current_env!r}. "
            f"Allowed: {allowed_envs}"
        )


class RouteDisabledException(ShieldException):
    """Raised when a route has been permanently disabled."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(reason)


class RouteProtectedException(ShieldException):
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
