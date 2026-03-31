"""Tests for waygate.core.models."""

from datetime import UTC, datetime

from waygate.core.models import AuditEntry, MaintenanceWindow, RouteState, RouteStatus


def test_route_status_values():
    assert RouteStatus.ACTIVE == "active"
    assert RouteStatus.MAINTENANCE == "maintenance"
    assert RouteStatus.DISABLED == "disabled"
    assert RouteStatus.ENV_GATED == "env_gated"
    assert RouteStatus.DEPRECATED == "deprecated"


def test_route_status_is_str():
    assert isinstance(RouteStatus.ACTIVE, str)


def test_maintenance_window():
    now = datetime.now(UTC)
    end = datetime(2025, 3, 10, 4, 0, 0, tzinfo=UTC)
    window = MaintenanceWindow(start=now, end=end, reason="DB migration")
    assert window.reason == "DB migration"
    assert window.start == now
    assert window.end == end


def test_maintenance_window_default_reason():
    now = datetime.now(UTC)
    window = MaintenanceWindow(start=now, end=now)
    assert window.reason == ""


def test_route_state_defaults():
    state = RouteState(path="/api/test")
    assert state.path == "/api/test"
    assert state.status == RouteStatus.ACTIVE
    assert state.reason == ""
    assert state.allowed_envs == []
    assert state.allowed_roles == []
    assert state.allowed_ips == []
    assert state.window is None
    assert state.sunset_date is None
    assert state.successor_path is None
    assert state.rollout_percentage == 100


def test_route_state_with_window():
    now = datetime.now(UTC)
    window = MaintenanceWindow(start=now, end=now, reason="test")
    state = RouteState(
        path="/api/payments",
        status=RouteStatus.MAINTENANCE,
        reason="DB migration",
        window=window,
    )
    assert state.status == RouteStatus.MAINTENANCE
    assert state.window is not None
    assert state.window.reason == "test"


def test_route_state_model_dump():
    state = RouteState(path="/api/test", status=RouteStatus.DISABLED, reason="gone")
    data = state.model_dump()
    assert data["path"] == "/api/test"
    assert data["status"] == RouteStatus.DISABLED


def test_route_state_model_validate():
    data = {"path": "/api/test", "status": "maintenance", "reason": "test"}
    state = RouteState.model_validate(data)
    assert state.status == RouteStatus.MAINTENANCE


def test_audit_entry():
    entry = AuditEntry(
        id="abc-123",
        timestamp=datetime.now(UTC),
        path="/api/payments",
        action="disable",
        actor="admin",
        reason="security",
        previous_status=RouteStatus.ACTIVE,
        new_status=RouteStatus.DISABLED,
    )
    assert entry.id == "abc-123"
    assert entry.actor == "admin"
    assert entry.previous_status == RouteStatus.ACTIVE
    assert entry.new_status == RouteStatus.DISABLED


def test_audit_entry_default_actor():
    entry = AuditEntry(
        id="abc-123",
        timestamp=datetime.now(UTC),
        path="/api/test",
        action="enable",
        previous_status=RouteStatus.MAINTENANCE,
        new_status=RouteStatus.ACTIVE,
    )
    assert entry.actor == "system"
    assert entry.reason == ""


def test_audit_entry_model_dump():
    entry = AuditEntry(
        id="abc-123",
        timestamp=datetime.now(UTC),
        path="/api/test",
        action="enable",
        previous_status=RouteStatus.MAINTENANCE,
        new_status=RouteStatus.ACTIVE,
    )
    data = entry.model_dump()
    assert data["id"] == "abc-123"
    assert data["path"] == "/api/test"
