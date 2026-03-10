"""Tests for ShieldEngine — every public method has a unit test."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from shield.core.models import MaintenanceWindow, RouteState, RouteStatus


@pytest.fixture
def engine() -> ShieldEngine:
    return ShieldEngine(backend=MemoryBackend(), current_env="production")


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------


async def test_check_unregistered_path_passes(engine):
    """Unregistered paths pass through (no state = ACTIVE)."""
    await engine.check("/unknown")  # must not raise


async def test_check_active_route_passes(engine):
    await engine.backend.set_state("/api/test", RouteState(path="/api/test"))
    await engine.check("/api/test")  # must not raise


async def test_check_maintenance_raises(engine):
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, reason="DB")
    await engine.backend.set_state("/api/pay", state)
    with pytest.raises(MaintenanceException) as exc_info:
        await engine.check("/api/pay")
    assert exc_info.value.reason == "DB"


async def test_check_maintenance_sets_retry_after(engine):
    window = MaintenanceWindow(
        start=datetime(2025, 3, 10, 2, 0, tzinfo=UTC),
        end=datetime(2025, 3, 10, 4, 0, tzinfo=UTC),
    )
    state = RouteState(
        path="/api/pay", status=RouteStatus.MAINTENANCE, window=window
    )
    await engine.backend.set_state("/api/pay", state)
    with pytest.raises(MaintenanceException) as exc_info:
        await engine.check("/api/pay")
    assert exc_info.value.retry_after == window.end


async def test_check_disabled_raises(engine):
    state = RouteState(
        path="/api/old", status=RouteStatus.DISABLED, reason="gone"
    )
    await engine.backend.set_state("/api/old", state)
    with pytest.raises(RouteDisabledException) as exc_info:
        await engine.check("/api/old")
    assert exc_info.value.reason == "gone"


async def test_check_env_gated_wrong_env_raises(engine):
    """Production engine → ENV_GATED route restricted to dev raises."""
    state = RouteState(
        path="/api/debug",
        status=RouteStatus.ENV_GATED,
        allowed_envs=["dev", "staging"],
    )
    await engine.backend.set_state("/api/debug", state)
    with pytest.raises(EnvGatedException) as exc_info:
        await engine.check("/api/debug")
    assert exc_info.value.current_env == "production"


async def test_check_env_gated_correct_env_passes():
    """Dev engine → ENV_GATED route restricted to dev passes."""
    engine = ShieldEngine(backend=MemoryBackend(), current_env="dev")
    state = RouteState(
        path="/api/debug",
        status=RouteStatus.ENV_GATED,
        allowed_envs=["dev"],
    )
    await engine.backend.set_state("/api/debug", state)
    await engine.check("/api/debug")  # must not raise


async def test_check_deprecated_passes(engine):
    """Deprecated routes still serve requests."""
    state = RouteState(path="/api/v1/users", status=RouteStatus.DEPRECATED)
    await engine.backend.set_state("/api/v1/users", state)
    await engine.check("/api/v1/users")  # must not raise


async def test_check_fail_open_on_backend_error(engine, caplog):
    """Backend errors must be logged and requests allowed through."""
    import logging

    class BrokenBackend(MemoryBackend):
        async def get_state(self, path):
            raise RuntimeError("Redis is down")

    broken_engine = ShieldEngine(backend=BrokenBackend())
    with caplog.at_level(logging.ERROR, logger="shield.core.engine"):
        await broken_engine.check("/api/test")  # must not raise
    log = caplog.text.lower()
    assert "backend error" in log or "redis is down" in log


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


async def test_register_creates_state(engine):
    await engine.register("/api/pay", {"status": "maintenance", "reason": "DB"})
    state = await engine.backend.get_state("/api/pay")
    assert state.status == RouteStatus.MAINTENANCE
    assert state.reason == "DB"


async def test_register_defaults_to_active(engine):
    await engine.register("/api/health", {})
    state = await engine.backend.get_state("/api/health")
    assert state.status == RouteStatus.ACTIVE


async def test_register_env_gated(engine):
    await engine.register(
        "/api/debug", {"status": "env_gated", "allowed_envs": ["dev"]}
    )
    state = await engine.backend.get_state("/api/debug")
    assert state.status == RouteStatus.ENV_GATED
    assert state.allowed_envs == ["dev"]


# ---------------------------------------------------------------------------
# enable()
# ---------------------------------------------------------------------------


async def test_enable_sets_active(engine):
    await engine.disable("/api/pay", reason="gone")
    result = await engine.enable("/api/pay", actor="admin")
    assert result.status == RouteStatus.ACTIVE
    assert result.reason == ""


async def test_enable_writes_audit(engine):
    await engine.disable("/api/pay")
    await engine.enable("/api/pay", actor="admin")
    log = await engine.get_audit_log("/api/pay")
    actions = [e.action for e in log]
    assert "enable" in actions


# ---------------------------------------------------------------------------
# disable()
# ---------------------------------------------------------------------------


async def test_disable_sets_disabled(engine):
    result = await engine.disable("/api/pay", reason="migration", actor="admin")
    assert result.status == RouteStatus.DISABLED
    assert result.reason == "migration"


async def test_disable_writes_audit(engine):
    await engine.disable("/api/pay", reason="gone", actor="ops")
    log = await engine.get_audit_log("/api/pay")
    assert log[0].action == "disable"
    assert log[0].actor == "ops"


# ---------------------------------------------------------------------------
# set_maintenance()
# ---------------------------------------------------------------------------


async def test_set_maintenance(engine):
    result = await engine.set_maintenance("/api/pay", reason="DB mig", actor="sys")
    assert result.status == RouteStatus.MAINTENANCE
    assert result.reason == "DB mig"


async def test_set_maintenance_with_window(engine):
    window = MaintenanceWindow(
        start=datetime(2025, 3, 10, 2, 0, tzinfo=UTC),
        end=datetime(2025, 3, 10, 4, 0, tzinfo=UTC),
    )
    result = await engine.set_maintenance("/api/pay", window=window)
    assert result.window is not None
    assert result.window.end == window.end


async def test_set_maintenance_writes_audit(engine):
    await engine.set_maintenance("/api/pay", reason="DB")
    log = await engine.get_audit_log("/api/pay")
    assert log[0].action == "maintenance_on"
    assert log[0].new_status == RouteStatus.MAINTENANCE


# ---------------------------------------------------------------------------
# set_env_only()
# ---------------------------------------------------------------------------


async def test_set_env_only(engine):
    result = await engine.set_env_only("/api/debug", ["dev", "staging"])
    assert result.status == RouteStatus.ENV_GATED
    assert result.allowed_envs == ["dev", "staging"]


async def test_set_env_only_writes_audit(engine):
    await engine.set_env_only("/api/debug", ["dev"])
    log = await engine.get_audit_log("/api/debug")
    assert log[0].action == "env_gate"


# ---------------------------------------------------------------------------
# get_state()
# ---------------------------------------------------------------------------


async def test_get_state_returns_active_for_unknown(engine):
    state = await engine.get_state("/unknown")
    assert state.status == RouteStatus.ACTIVE
    assert state.path == "/unknown"


async def test_get_state_returns_registered_state(engine):
    await engine.disable("/api/pay")
    state = await engine.get_state("/api/pay")
    assert state.status == RouteStatus.DISABLED


# ---------------------------------------------------------------------------
# list_states()
# ---------------------------------------------------------------------------


async def test_list_states_empty(engine):
    states = await engine.list_states()
    assert states == []


async def test_list_states_returns_all(engine):
    await engine.disable("/api/a")
    await engine.disable("/api/b")
    states = await engine.list_states()
    paths = {s.path for s in states}
    assert paths == {"/api/a", "/api/b"}


# ---------------------------------------------------------------------------
# get_audit_log()
# ---------------------------------------------------------------------------


async def test_get_audit_log_all(engine):
    await engine.disable("/api/a")
    await engine.disable("/api/b")
    log = await engine.get_audit_log()
    assert len(log) == 2


async def test_get_audit_log_filtered(engine):
    await engine.disable("/api/a")
    await engine.disable("/api/b")
    log = await engine.get_audit_log("/api/a")
    assert all(e.path == "/api/a" for e in log)


# ---------------------------------------------------------------------------
# Regression: register() must not overwrite persisted state (restart safety)
# ---------------------------------------------------------------------------


async def test_register_does_not_overwrite_persisted_state():
    """Bug regression: re-registering a route on restart must not undo CLI changes.

    Scenario:
      1. Server starts → decorator says MAINTENANCE → registered.
      2. CLI: shield enable /api/pay → state = ACTIVE in backend.
      3. Server restarts → register() called again with maintenance meta.
      4. Expected: state remains ACTIVE (persisted state wins).
      5. Old behaviour: state reset back to MAINTENANCE (bug).
    """
    from shield.core.backends.memory import MemoryBackend
    from shield.core.engine import ShieldEngine

    engine = ShieldEngine(backend=MemoryBackend())

    # Simulate first startup: decorator registers MAINTENANCE.
    await engine.register("/api/pay", {"status": "maintenance", "reason": "DB"})
    assert (await engine.get_state("/api/pay")).status == RouteStatus.MAINTENANCE

    # Operator enables the route via CLI / admin.
    await engine.enable("/api/pay", actor="ops")
    assert (await engine.get_state("/api/pay")).status == RouteStatus.ACTIVE

    # Simulate server restart: register() called again with same decorator meta.
    await engine.register("/api/pay", {"status": "maintenance", "reason": "DB"})

    # Persisted ACTIVE state must survive — NOT reset to MAINTENANCE.
    state = await engine.get_state("/api/pay")
    assert state.status == RouteStatus.ACTIVE, (
        "register() overwrote persisted state — restart wiped CLI changes"
    )


async def test_register_applies_decorator_when_no_persisted_state():
    """On first startup (empty backend) decorator state is always applied."""
    from shield.core.backends.memory import MemoryBackend
    from shield.core.engine import ShieldEngine

    engine = ShieldEngine(backend=MemoryBackend())

    await engine.register("/api/new", {"status": "disabled", "reason": "beta"})
    state = await engine.get_state("/api/new")
    assert state.status == RouteStatus.DISABLED


# ---------------------------------------------------------------------------
# force_active protection
# ---------------------------------------------------------------------------


async def test_force_active_registered_with_flag(engine):
    """@force_active routes are stored with force_active=True in backend."""
    await engine.register("/health", {"force_active": True})
    state = await engine.backend.get_state("/health")
    assert state.force_active is True
    assert state.status == RouteStatus.ACTIVE


async def test_force_active_disable_raises(engine):
    from shield.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.disable("/health", reason="test")


async def test_force_active_enable_raises(engine):
    from shield.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.enable("/health")


async def test_force_active_set_maintenance_raises(engine):
    from shield.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.set_maintenance("/health", reason="test")


async def test_force_active_set_env_only_raises(engine):
    from shield.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.set_env_only("/health", ["dev"])


async def test_force_active_schedule_maintenance_raises(engine):

    from shield.core.exceptions import RouteProtectedException
    from shield.core.models import MaintenanceWindow

    await engine.register("/health", {"force_active": True})
    window = MaintenanceWindow(
        start=datetime(2030, 1, 1, tzinfo=UTC),
        end=datetime(2030, 1, 2, tzinfo=UTC),
    )
    with pytest.raises(RouteProtectedException):
        await engine.schedule_maintenance("/health", window)


async def test_non_force_active_route_is_mutable(engine):
    """Normal routes are not protected."""
    await engine.register("/api/pay", {"status": "active"})
    state = await engine.disable("/api/pay", reason="test")
    assert state.status == RouteStatus.DISABLED


async def test_force_active_state_preserved_on_restart(engine):
    """force_active flag survives re-registration (persistence-first)."""
    await engine.register("/health", {"force_active": True})
    # Re-register with force_active=False — persisted state must win.
    await engine.register("/health", {"force_active": False})
    state = await engine.backend.get_state("/health")
    assert state.force_active is True
