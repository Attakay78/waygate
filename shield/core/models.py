"""Core data models for api-shield."""

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


class AuditEntry(BaseModel):
    """An immutable record of a route state change."""

    id: str  # uuid4
    timestamp: datetime
    path: str
    action: str
    actor: str = "system"
    reason: str = ""
    previous_status: RouteStatus
    new_status: RouteStatus
