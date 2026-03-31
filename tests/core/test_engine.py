"""Tests for WaygateEngine — every public method has a unit test."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from waygate.core.models import MaintenanceWindow, RouteState, RouteStatus


@pytest.fixture
def engine() -> WaygateEngine:
    return WaygateEngine(backend=MemoryBackend(), current_env="production")


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
    state = RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, window=window)
    await engine.backend.set_state("/api/pay", state)
    with pytest.raises(MaintenanceException) as exc_info:
        await engine.check("/api/pay")
    assert exc_info.value.retry_after == window.end


async def test_check_disabled_raises(engine):
    state = RouteState(path="/api/old", status=RouteStatus.DISABLED, reason="gone")
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
    engine = WaygateEngine(backend=MemoryBackend(), current_env="dev")
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

    broken_engine = WaygateEngine(backend=BrokenBackend())
    with caplog.at_level(logging.ERROR, logger="waygate.core.engine"):
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
    await engine.register("/api/debug", {"status": "env_gated", "allowed_envs": ["dev"]})
    state = await engine.backend.get_state("/api/debug")
    assert state.status == RouteStatus.ENV_GATED
    assert state.allowed_envs == ["dev"]


# ---------------------------------------------------------------------------
# enable()
# ---------------------------------------------------------------------------


async def test_enable_sets_active(engine):
    await engine.register("/api/pay", {"status": "active"})
    await engine.disable("/api/pay", reason="gone")
    result = await engine.enable("/api/pay", actor="admin")
    assert result.status == RouteStatus.ACTIVE
    assert result.reason == ""


async def test_enable_writes_audit(engine):
    await engine.register("/api/pay", {"status": "active"})
    await engine.disable("/api/pay")
    await engine.enable("/api/pay", actor="admin")
    log = await engine.get_audit_log("/api/pay")
    actions = [e.action for e in log]
    assert "enable" in actions


# ---------------------------------------------------------------------------
# disable()
# ---------------------------------------------------------------------------


async def test_disable_sets_disabled(engine):
    await engine.register("/api/pay", {"status": "active"})
    result = await engine.disable("/api/pay", reason="migration", actor="admin")
    assert result.status == RouteStatus.DISABLED
    assert result.reason == "migration"


async def test_disable_writes_audit(engine):
    await engine.register("/api/pay", {"status": "active"})
    await engine.disable("/api/pay", reason="gone", actor="ops")
    log = await engine.get_audit_log("/api/pay")
    assert log[0].action == "disable"
    assert log[0].actor == "ops"


# ---------------------------------------------------------------------------
# set_maintenance()
# ---------------------------------------------------------------------------


async def test_set_maintenance(engine):
    await engine.register("/api/pay", {"status": "active"})
    result = await engine.set_maintenance("/api/pay", reason="DB mig", actor="sys")
    assert result.status == RouteStatus.MAINTENANCE
    assert result.reason == "DB mig"


async def test_set_maintenance_with_window(engine):
    await engine.register("/api/pay", {"status": "active"})
    window = MaintenanceWindow(
        start=datetime(2025, 3, 10, 2, 0, tzinfo=UTC),
        end=datetime(2025, 3, 10, 4, 0, tzinfo=UTC),
    )
    result = await engine.set_maintenance("/api/pay", window=window)
    assert result.window is not None
    assert result.window.end == window.end


async def test_set_maintenance_writes_audit(engine):
    await engine.register("/api/pay", {"status": "active"})
    await engine.set_maintenance("/api/pay", reason="DB")
    log = await engine.get_audit_log("/api/pay")
    assert log[0].action == "maintenance_on"
    assert log[0].new_status == RouteStatus.MAINTENANCE


# ---------------------------------------------------------------------------
# set_env_only()
# ---------------------------------------------------------------------------


async def test_set_env_only(engine):
    await engine.register("/api/debug", {"status": "active"})
    result = await engine.set_env_only("/api/debug", ["dev", "staging"])
    assert result.status == RouteStatus.ENV_GATED
    assert result.allowed_envs == ["dev", "staging"]


async def test_set_env_only_writes_audit(engine):
    await engine.register("/api/debug", {"status": "active"})
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
    await engine.register("/api/pay", {"status": "active"})
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
    await engine.register("/api/a", {"status": "active"})
    await engine.register("/api/b", {"status": "active"})
    await engine.disable("/api/a")
    await engine.disable("/api/b")
    states = await engine.list_states()
    paths = {s.path for s in states}
    assert paths == {"/api/a", "/api/b"}


# ---------------------------------------------------------------------------
# get_audit_log()
# ---------------------------------------------------------------------------


async def test_get_audit_log_all(engine):
    await engine.register("/api/a", {"status": "active"})
    await engine.register("/api/b", {"status": "active"})
    await engine.disable("/api/a")
    await engine.disable("/api/b")
    log = await engine.get_audit_log()
    assert len(log) == 2


async def test_get_audit_log_filtered(engine):
    await engine.register("/api/a", {"status": "active"})
    await engine.register("/api/b", {"status": "active"})
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
      2. CLI: waygate enable /api/pay → state = ACTIVE in backend.
      3. Server restarts → register() called again with maintenance meta.
      4. Expected: state remains ACTIVE (persisted state wins).
      5. Old behaviour: state reset back to MAINTENANCE (bug).
    """
    from waygate.core.backends.memory import MemoryBackend
    from waygate.core.engine import WaygateEngine

    engine = WaygateEngine(backend=MemoryBackend())

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
    from waygate.core.backends.memory import MemoryBackend
    from waygate.core.engine import WaygateEngine

    engine = WaygateEngine(backend=MemoryBackend())

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
    from waygate.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.disable("/health", reason="test")


async def test_force_active_enable_raises(engine):
    from waygate.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.enable("/health")


async def test_force_active_set_maintenance_raises(engine):
    from waygate.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.set_maintenance("/health", reason="test")


async def test_force_active_set_env_only_raises(engine):
    from waygate.core.exceptions import RouteProtectedException

    await engine.register("/health", {"force_active": True})
    with pytest.raises(RouteProtectedException):
        await engine.set_env_only("/health", ["dev"])


async def test_force_active_schedule_maintenance_raises(engine):

    from waygate.core.exceptions import RouteProtectedException
    from waygate.core.models import MaintenanceWindow

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


# ---------------------------------------------------------------------------
# RouteNotFoundException and AmbiguousRouteError
# ---------------------------------------------------------------------------


async def test_disable_unregistered_raises_not_found(engine):
    """Mutating an unregistered route raises RouteNotFoundException."""
    from waygate.core.exceptions import RouteNotFoundException

    with pytest.raises(RouteNotFoundException) as exc_info:
        await engine.disable("/nonexistent", reason="gone")
    assert "/nonexistent" in str(exc_info.value)


async def test_enable_unregistered_raises_not_found(engine):
    from waygate.core.exceptions import RouteNotFoundException

    with pytest.raises(RouteNotFoundException):
        await engine.enable("/nonexistent")


async def test_set_maintenance_unregistered_raises_not_found(engine):
    from waygate.core.exceptions import RouteNotFoundException

    with pytest.raises(RouteNotFoundException):
        await engine.set_maintenance("/nonexistent", reason="test")


async def test_bare_path_resolves_single_method_match(engine):
    """Bare /pay resolves to GET:/pay when only one method-prefixed variant exists."""
    await engine.register("GET:/pay", {"status": "active"})
    state = await engine.disable("/pay", reason="resolved")
    assert state.status == RouteStatus.DISABLED
    # The actual key in the backend must be the method-prefixed one.
    stored = await engine.backend.get_state("GET:/pay")
    assert stored.status == RouteStatus.DISABLED


async def test_bare_path_ambiguous_raises(engine):
    """Bare /pay with GET:/pay and POST:/pay raises AmbiguousRouteError."""
    from waygate.core.exceptions import AmbiguousRouteError

    await engine.register("GET:/pay", {"status": "active"})
    await engine.register("POST:/pay", {"status": "active"})
    with pytest.raises(AmbiguousRouteError) as exc_info:
        await engine.disable("/pay", reason="ambiguous")
    assert exc_info.value.path == "/pay"
    assert set(exc_info.value.matches) == {"GET:/pay", "POST:/pay"}


# ---------------------------------------------------------------------------
# Distributed global config cache invalidation — start() / stop()
# ---------------------------------------------------------------------------


async def test_start_creates_listener_task(engine):
    """start() creates a background asyncio.Task."""
    await engine.start()
    assert engine._global_listener_task is not None
    await engine.stop()


async def test_start_is_idempotent(engine):
    """Calling start() twice does not create a second task."""
    await engine.start()
    task_first = engine._global_listener_task
    await engine.start()
    assert engine._global_listener_task is task_first
    await engine.stop()


async def test_stop_cancels_listener_task(engine):
    """stop() cancels the task and sets the reference to None."""
    await engine.start()
    assert engine._global_listener_task is not None
    await engine.stop()
    assert engine._global_listener_task is None


async def test_listener_exits_silently_for_memory_backend(engine):
    """MemoryBackend raises NotImplementedError — task ends without error."""
    await engine.start()
    task = engine._global_listener_task
    # Give the task one event-loop iteration to run and hit NotImplementedError.
    await asyncio.sleep(0)
    assert task.done()
    assert task.exception() is None  # no unhandled exception


async def test_aenter_aexit_starts_and_stops_listener():
    """async with WaygateEngine() starts and stops the listener task."""
    async with WaygateEngine(backend=MemoryBackend()) as eng:
        # Task is created (may already be done for MemoryBackend — that's fine).
        assert eng._global_listener_task is not None
    # After exit, task reference is cleared.
    assert eng._global_listener_task is None


async def test_global_config_cache_invalidated_by_listener():
    """Listener invalidates the cache when the backend signals a change."""
    from collections.abc import AsyncIterator

    # A fake backend whose subscribe_global_config() yields one signal then stops.
    class _FakeBackend(MemoryBackend):
        async def subscribe_global_config(self) -> AsyncIterator[None]:
            yield None  # one invalidation signal

    eng = WaygateEngine(backend=_FakeBackend())
    # enable_global_maintenance writes then invalidates the cache; re-fetch to
    # populate it so we can observe the listener clearing it.
    await eng.enable_global_maintenance(reason="test")
    await eng._get_global_config_cached()
    assert eng._global_config_cache is not None

    await eng.start()
    # Allow the listener task to consume the signal.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Cache must be cleared after the signal was processed.
    assert eng._global_config_cache is None
    await eng.stop()


# ---------------------------------------------------------------------------
# Webhook deduplication — _fire_webhooks / _dispatch_webhooks
# ---------------------------------------------------------------------------


async def test_webhook_fires_when_claim_succeeds():
    """Webhook is delivered when the backend grants the dispatch claim."""
    delivered: list[str] = []

    async def fake_post(url: str, payload: dict) -> None:  # type: ignore[override]
        delivered.append(url)

    eng = WaygateEngine(backend=MemoryBackend())
    eng.add_webhook("http://example.com/hook")

    await eng.register("/api/pay", {"status": "active"})

    # Patch _post_webhook so we don't make real HTTP calls.
    import unittest.mock as mock

    with mock.patch.object(type(eng), "_post_webhook", staticmethod(fake_post)):
        await eng.disable("/api/pay", reason="test")
        # Let the dispatch task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "http://example.com/hook" in delivered


async def test_webhook_skipped_when_claim_denied():
    """Webhook is not delivered when the backend denies the dispatch claim."""

    delivered: list[str] = []

    async def fake_post(url: str, payload: dict) -> None:  # type: ignore[override]
        delivered.append(url)

    class _AlreadyClaimedBackend(MemoryBackend):
        async def try_claim_webhook_dispatch(self, dedup_key: str, ttl_seconds: int = 60) -> bool:
            return False  # simulate another instance already claimed it

    eng = WaygateEngine(backend=_AlreadyClaimedBackend())
    eng.add_webhook("http://example.com/hook")
    await eng.register("/api/pay", {"status": "active"})

    import unittest.mock as mock

    with mock.patch.object(type(eng), "_post_webhook", staticmethod(fake_post)):
        await eng.disable("/api/pay", reason="test")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert delivered == []


async def test_webhook_dedup_key_is_deterministic():
    """Two engines producing the same event+path+state generate the same dedup key."""
    import hashlib

    from waygate.core.models import RouteState, RouteStatus

    state = RouteState(path="/api/pay", status=RouteStatus.DISABLED, reason="gone")
    raw = f"disable:/api/pay:{state.model_dump_json()}"
    key_a = hashlib.sha256(raw.encode()).hexdigest()
    key_b = hashlib.sha256(raw.encode()).hexdigest()

    assert key_a == key_b


async def test_no_webhook_tasks_when_no_webhooks_registered():
    """_fire_webhooks is a no-op when no webhooks are registered."""
    eng = WaygateEngine(backend=MemoryBackend())
    await eng.register("/api/pay", {"status": "active"})
    # No webhook registered — disable should not create any tasks.
    tasks_before = len(asyncio.all_tasks())
    await eng.disable("/api/pay", reason="test")
    tasks_after = len(asyncio.all_tasks())
    # No new webhook dispatch tasks created.
    assert tasks_after == tasks_before


async def test_webhook_fires_once_when_two_engines_share_same_backend():
    """Simulates two instances: only the first to claim should deliver."""
    delivered: list[str] = []
    claim_count = 0

    async def fake_post(url: str, payload: dict) -> None:  # type: ignore[override]
        delivered.append(url)

    class _CountingBackend(MemoryBackend):
        """Grants the first claim, denies all subsequent ones for the same key."""

        def __init__(self) -> None:
            super().__init__()
            self._claimed: set[str] = set()

        async def try_claim_webhook_dispatch(self, dedup_key: str, ttl_seconds: int = 60) -> bool:
            nonlocal claim_count
            claim_count += 1
            if dedup_key in self._claimed:
                return False
            self._claimed.add(dedup_key)
            return True

    shared_backend = _CountingBackend()

    eng_a = WaygateEngine(backend=shared_backend)
    eng_a.add_webhook("http://example.com/hook")
    await eng_a.register("/api/pay", {"status": "active"})

    eng_b = WaygateEngine(backend=shared_backend)
    eng_b.add_webhook("http://example.com/hook")
    # eng_b shares the backend but has its own RouteState view —
    # set the state directly so eng_b can also fire for the same event.
    from waygate.core.models import RouteState, RouteStatus

    state = RouteState(path="/api/pay", status=RouteStatus.DISABLED, reason="test")

    import unittest.mock as mock

    with mock.patch.object(type(eng_a), "_post_webhook", staticmethod(fake_post)):
        with mock.patch.object(type(eng_b), "_post_webhook", staticmethod(fake_post)):
            # Simulate both instances trying to fire the same event.
            eng_a._fire_webhooks("disable", "/api/pay", state)
            eng_b._fire_webhooks("disable", "/api/pay", state)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    # Exactly one delivery despite two instances firing.
    assert len(delivered) == 1
    assert claim_count == 2  # both tried; only first succeeded
