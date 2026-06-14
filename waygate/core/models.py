"""Core data models for waygate."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RouteStatus(StrEnum):
    """Possible lifecycle states for a route."""

    ACTIVE = "active"
    """The route is live and accepting requests. This is the default state."""

    MAINTENANCE = "maintenance"
    """The route is temporarily unavailable. Requests receive a 503 response with
    an optional ``Retry-After`` header when a ``MaintenanceWindow`` is set."""

    DISABLED = "disabled"
    """The route has been permanently disabled. Requests receive a 503 response."""

    ENV_GATED = "env_gated"
    """The route is restricted to specific environments. Requests from other
    environments receive a 403 response."""

    DEPRECATED = "deprecated"
    """The route still serves requests but is scheduled for removal.
    Responses include ``Deprecation``, ``Sunset``, and ``Link`` headers."""


class MaintenanceWindow(BaseModel):
    """A scheduled maintenance window with start and end times.

    Parameters
    ----------
    start:
        When the maintenance window begins (UTC-aware datetime).
    end:
        When the maintenance window ends.  Used as the value of the
        ``Retry-After`` header on 503 responses.
    reason:
        Human-readable explanation of the maintenance period.
    """

    start: datetime
    end: datetime
    reason: str = ""


class RouteState(BaseModel):
    """Full lifecycle state for a single route.

    Parameters
    ----------
    path:
        The route key as registered (e.g. ``"GET:/payments"`` or ``"/payments"``).
    service:
        Optional service name grouping this route.  Set by the Waygate SDK
        to associate routes with a named application service.
    status:
        Current lifecycle state.  Defaults to ``ACTIVE``.
    reason:
        Human-readable explanation of the current state, included in error
        responses and shown in the dashboard and CLI.
    allowed_envs:
        When ``status`` is ``ENV_GATED``, only requests arriving in one of
        these environment names are allowed through.
    allowed_roles:
        Reserved for future role-based allowlisting.
    allowed_ips:
        Reserved for future IP-based allowlisting.
    window:
        Scheduled maintenance window.  Set by ``schedule_maintenance()`` and
        used to supply the ``Retry-After`` header value.
    sunset_date:
        RFC 7231 or ISO-8601 date string injected into the ``Sunset`` header
        when the route is deprecated.
    successor_path:
        Replacement endpoint path injected as the ``Link`` header value when
        the route is deprecated.
    rollout_percentage:
        Reserved for future gradual rollout support.  Currently always ``100``.
    force_active:
        When ``True``, all state mutations are rejected.  Set by the
        ``@force_active`` decorator.
    """

    path: str
    service: str | None = None
    status: RouteStatus = RouteStatus.ACTIVE
    reason: str = ""
    allowed_envs: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=list)
    window: MaintenanceWindow | None = None
    sunset_date: str | None = None
    successor_path: str | None = None
    rollout_percentage: int = 100
    force_active: bool = False


class GlobalMaintenanceConfig(BaseModel):
    """Configuration for the global maintenance mode switch.

    When ``enabled`` is ``True``, *every* route returns 503 unless it is
    explicitly listed in ``exempt_paths``.

    Parameters
    ----------
    enabled:
        Whether global maintenance mode is active.
    reason:
        Human-readable explanation shown in all 503 responses.
    exempt_paths:
        Route keys that bypass global maintenance.  Accepts both bare paths
        (``"/health"``) and method-prefixed keys (``"GET:/health"``).
    include_force_active:
        When ``False`` (default) ``@force_active`` routes are always exempt —
        health checks and status endpoints remain reachable during global
        maintenance.  Set to ``True`` to block *everything*, including
        force-active routes.
    """

    enabled: bool = False
    reason: str = ""
    exempt_paths: list[str] = Field(default_factory=list)
    include_force_active: bool = False

    @property
    def exempt_set(self) -> frozenset[str]:
        """Return ``exempt_paths`` as a frozenset for O(1) membership tests.

        Constructed once per config object — avoids rebuilding a set on every
        call to ``engine.check()`` when global maintenance is active.
        """
        return frozenset(self.exempt_paths)


class AuditEntry(BaseModel):
    """An immutable record of a state change.

    Route lifecycle changes populate ``previous_status`` and ``new_status``.
    Rate limit policy mutations (set, delete, reset) leave those fields
    ``None`` and store relevant detail in ``reason``.

    Parameters
    ----------
    id:
        UUID4 identifier for this entry.
    timestamp:
        When the change was recorded (UTC-aware).
    path:
        The route key that was changed, or an internal sentinel path for
        rate-limit and global-maintenance audit entries.
    service:
        Mirrors ``RouteState.service`` so audit rows can be filtered by
        service name.
    action:
        Short identifier for the operation (e.g. ``"enable"``,
        ``"maintenance_on"``, ``"global_rl_set"``).
    actor:
        Identity of the caller that triggered the change.  Defaults to
        ``"system"`` for automated operations.
    platform:
        Where the change originated: ``"cli"``, ``"dashboard"``, or
        ``"system"``.
    reason:
        Human-readable context for the change.  Included in error responses
        and the audit log view.
    previous_status:
        Route lifecycle status before the change.  ``None`` for non-lifecycle
        mutations such as rate limit policy changes.
    new_status:
        Route lifecycle status after the change.  ``None`` for non-lifecycle
        mutations.
    """

    id: str
    timestamp: datetime
    path: str
    service: str | None = None
    action: str
    actor: str = "system"
    platform: str = "system"
    reason: str = ""
    previous_status: RouteStatus | None = None
    new_status: RouteStatus | None = None
