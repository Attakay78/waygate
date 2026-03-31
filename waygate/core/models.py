"""Core data models for waygate."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RouteStatus(StrEnum):
    """Possible lifecycle states for a route."""

    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DISABLED = "disabled"
    ENV_GATED = "env_gated"
    DEPRECATED = "deprecated"


class MaintenanceWindow(BaseModel):
    """A scheduled maintenance window with start and end times."""

    start: datetime
    end: datetime
    reason: str = ""


class RouteState(BaseModel):
    """Full lifecycle state for a single route."""

    path: str
    service: str | None = None  # set by WaygateSDK to group routes by service name
    status: RouteStatus = RouteStatus.ACTIVE
    reason: str = ""
    allowed_envs: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=list)
    window: MaintenanceWindow | None = None
    sunset_date: str | None = None  # RFC 7231 or ISO-8601 string for Sunset header
    successor_path: str | None = None
    rollout_percentage: int = 100
    force_active: bool = False  # if True, all state mutations are rejected


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

    Route lifecycle changes populate ``previous_status`` / ``new_status``.
    Rate limit policy mutations (set, delete, reset) leave those fields
    ``None`` and store relevant detail in ``reason``.
    """

    id: str  # uuid4
    timestamp: datetime
    path: str
    service: str | None = None  # mirrors RouteState.service for filtering
    action: str
    actor: str = "system"
    platform: str = "system"  # "cli", "dashboard", or "system"
    reason: str = ""
    previous_status: RouteStatus | None = None
    new_status: RouteStatus | None = None
