"""Tests for bypass_lifecycle, bypass_rate_limits, and waygate.testing.bypass."""

from __future__ import annotations

import pytest

from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from waygate.core.models import RouteState, RouteStatus
from waygate.testing import bypass


@pytest.fixture
def engine() -> WaygateEngine:
    return WaygateEngine(backend=MemoryBackend(), current_env="production")


# ---------------------------------------------------------------------------
# bypass_lifecycle constructor flag
# ---------------------------------------------------------------------------


async def test_bypass_lifecycle_skips_maintenance():
    """bypass_lifecycle=True lets maintenance routes through."""
    engine = WaygateEngine(backend=MemoryBackend(), bypass_lifecycle=True)
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, reason="DB")
    await engine.backend.set_state("/api/pay", state)
    await engine.check("/api/pay")  # must not raise


async def test_bypass_lifecycle_skips_disabled():
    """bypass_lifecycle=True lets disabled routes through."""
    engine = WaygateEngine(backend=MemoryBackend(), bypass_lifecycle=True)
    state = RouteState(path="/api/old", status=RouteStatus.DISABLED, reason="gone")
    await engine.backend.set_state("/api/old", state)
    await engine.check("/api/old")  # must not raise


async def test_bypass_lifecycle_skips_env_gated():
    """bypass_lifecycle=True lets env-gated routes through regardless of env."""
    engine = WaygateEngine(
        backend=MemoryBackend(),
        current_env="production",
        bypass_lifecycle=True,
    )
    state = RouteState(
        path="/api/debug",
        status=RouteStatus.ENV_GATED,
        allowed_envs=["dev"],
    )
    await engine.backend.set_state("/api/debug", state)
    await engine.check("/api/debug")  # must not raise despite wrong env


async def test_bypass_lifecycle_skips_global_maintenance():
    """bypass_lifecycle=True skips global maintenance."""
    engine = WaygateEngine(backend=MemoryBackend(), bypass_lifecycle=True)
    await engine.enable_global_maintenance(reason="fleet down")
    await engine.check("/api/payments")  # must not raise


async def test_bypass_lifecycle_false_still_enforces_maintenance(engine):
    """Default engine (bypass_lifecycle=False) still blocks maintenance routes."""
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, reason="DB")
    await engine.backend.set_state("/api/pay", state)
    with pytest.raises(MaintenanceException):
        await engine.check("/api/pay")


async def test_bypass_lifecycle_false_still_enforces_disabled(engine):
    """Default engine (bypass_lifecycle=False) still blocks disabled routes."""
    state = RouteState(path="/api/old", status=RouteStatus.DISABLED)
    await engine.backend.set_state("/api/old", state)
    with pytest.raises(RouteDisabledException):
        await engine.check("/api/old")


async def test_bypass_lifecycle_false_still_enforces_env_gated(engine):
    """Default engine (bypass_lifecycle=False) still blocks wrong-env routes."""
    state = RouteState(
        path="/api/debug",
        status=RouteStatus.ENV_GATED,
        allowed_envs=["dev"],
    )
    await engine.backend.set_state("/api/debug", state)
    with pytest.raises(EnvGatedException):
        await engine.check("/api/debug")


# ---------------------------------------------------------------------------
# bypass_rate_limits constructor flag
# ---------------------------------------------------------------------------


def test_bypass_rate_limits_default_is_false():
    engine = WaygateEngine()
    assert engine.bypass_rate_limits is False


def test_bypass_lifecycle_default_is_false():
    engine = WaygateEngine()
    assert engine.bypass_lifecycle is False


def test_bypass_rate_limits_kwarg_sets_flag():
    engine = WaygateEngine(bypass_rate_limits=True)
    assert engine.bypass_rate_limits is True


def test_bypass_lifecycle_kwarg_sets_flag():
    engine = WaygateEngine(bypass_lifecycle=True)
    assert engine.bypass_lifecycle is True


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


def test_bypass_lifecycle_env_var(monkeypatch):
    monkeypatch.setenv("WAYGATE_BYPASS_LIFECYCLE", "1")
    engine = WaygateEngine()
    assert engine.bypass_lifecycle is True


def test_bypass_rate_limits_env_var(monkeypatch):
    monkeypatch.setenv("WAYGATE_BYPASS_RATE_LIMITS", "1")
    engine = WaygateEngine()
    assert engine.bypass_rate_limits is True


def test_bypass_lifecycle_env_var_empty_string_does_not_set(monkeypatch):
    """An empty string env var must not activate the flag."""
    monkeypatch.setenv("WAYGATE_BYPASS_LIFECYCLE", "")
    engine = WaygateEngine()
    assert engine.bypass_lifecycle is False


def test_bypass_rate_limits_env_var_empty_string_does_not_set(monkeypatch):
    monkeypatch.setenv("WAYGATE_BYPASS_RATE_LIMITS", "")
    engine = WaygateEngine()
    assert engine.bypass_rate_limits is False


def test_env_var_takes_precedence_over_false_kwarg(monkeypatch):
    """Env var activates the flag even when the kwarg is False."""
    monkeypatch.setenv("WAYGATE_BYPASS_LIFECYCLE", "1")
    engine = WaygateEngine(bypass_lifecycle=False)
    assert engine.bypass_lifecycle is True


# ---------------------------------------------------------------------------
# waygate.testing.bypass context manager
# ---------------------------------------------------------------------------


def test_bypass_context_manager_sets_flags(engine):
    with bypass(engine):
        assert engine.bypass_rate_limits is True
        assert engine.bypass_lifecycle is True


def test_bypass_context_manager_restores_flags(engine):
    original_rl = engine.bypass_rate_limits
    original_lc = engine.bypass_lifecycle
    with bypass(engine):
        pass
    assert engine.bypass_rate_limits == original_rl
    assert engine.bypass_lifecycle == original_lc


def test_bypass_context_manager_restores_on_exception(engine):
    """Flags are restored even when the block raises."""
    original_rl = engine.bypass_rate_limits
    original_lc = engine.bypass_lifecycle
    with pytest.raises(ValueError):
        with bypass(engine):
            raise ValueError("boom")
    assert engine.bypass_rate_limits == original_rl
    assert engine.bypass_lifecycle == original_lc


def test_bypass_rate_limits_only(engine):
    """bypass(lifecycle=False) only toggles rate_limits."""
    with bypass(engine, rate_limits=True, lifecycle=False):
        assert engine.bypass_rate_limits is True
        assert engine.bypass_lifecycle is False


def test_bypass_lifecycle_only(engine):
    """bypass(rate_limits=False) only toggles lifecycle."""
    with bypass(engine, rate_limits=False, lifecycle=True):
        assert engine.bypass_rate_limits is False
        assert engine.bypass_lifecycle is True


async def test_bypass_context_manager_lifecycle_skips_maintenance(engine):
    """bypass(lifecycle=True) inside a test block lets maintenance routes through."""
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, reason="DB")
    await engine.backend.set_state("/api/pay", state)

    with pytest.raises(MaintenanceException):
        await engine.check("/api/pay")  # blocked before bypass

    with bypass(engine, lifecycle=True):
        await engine.check("/api/pay")  # must not raise

    with pytest.raises(MaintenanceException):
        await engine.check("/api/pay")  # blocked again after bypass exits


async def test_bypass_context_manager_does_not_affect_other_engines():
    """bypass() only modifies the engine it receives."""
    engine_a = WaygateEngine(backend=MemoryBackend())
    engine_b = WaygateEngine(backend=MemoryBackend())
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE)
    await engine_b.backend.set_state("/api/pay", state)

    with bypass(engine_a):
        with pytest.raises(MaintenanceException):
            await engine_b.check("/api/pay")
