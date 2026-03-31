"""Tests for MaintenanceScheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.core.models import MaintenanceWindow, RouteStatus
from waygate.core.scheduler import MaintenanceScheduler


@pytest.fixture
def engine() -> WaygateEngine:
    return WaygateEngine(backend=MemoryBackend())


@pytest.fixture
def scheduler(engine) -> MaintenanceScheduler:
    return MaintenanceScheduler(engine=engine)


# ---------------------------------------------------------------------------
# schedule() and list_scheduled()
# ---------------------------------------------------------------------------


async def test_schedule_creates_task(scheduler):
    window = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
    )
    await scheduler.schedule("/api/pay", window)
    scheduled = await scheduler.list_scheduled()
    paths = [p for p, _ in scheduled]
    assert "/api/pay" in paths


async def test_schedule_replaces_existing(scheduler):
    window1 = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
    )
    window2 = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=3),
        end=datetime.now(UTC) + timedelta(hours=4),
    )
    await scheduler.schedule("/api/pay", window1)
    await scheduler.schedule("/api/pay", window2)
    scheduled = await scheduler.list_scheduled()
    paths = [p for p, _ in scheduled]
    assert paths.count("/api/pay") == 1


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


async def test_cancel_removes_task(scheduler):
    window = MaintenanceWindow(
        start=datetime.now(UTC) + timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(hours=2),
    )
    await scheduler.schedule("/api/pay", window)
    await scheduler.cancel("/api/pay")
    scheduled = await scheduler.list_scheduled()
    assert not any(p == "/api/pay" for p, _ in scheduled)


async def test_cancel_noop_for_unknown(scheduler):
    """cancel() on an unscheduled path must not raise."""
    await scheduler.cancel("/not/scheduled")


# ---------------------------------------------------------------------------
# Window activation (near-future start)
# ---------------------------------------------------------------------------


async def test_window_activates_at_start(engine, scheduler):
    """A window with start in 50ms activates maintenance within 200ms."""
    await engine.register("/api/pay", {"status": "active"})
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now + timedelta(milliseconds=50),
        end=now + timedelta(hours=1),
        reason="scheduled test",
    )
    await scheduler.schedule("/api/pay", window)

    # Wait for the task to fire.
    await asyncio.sleep(0.2)

    state = await engine.backend.get_state("/api/pay")
    assert state.status == RouteStatus.MAINTENANCE
    assert state.reason == "scheduled test"


async def test_window_deactivates_at_end(engine, scheduler):
    """A window that starts immediately and ends in 100ms re-enables the route."""
    await engine.register("/api/pay", {"status": "active"})
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now - timedelta(seconds=1),  # start in the past → fires immediately
        end=now + timedelta(milliseconds=100),
        reason="short window",
    )
    await scheduler.schedule("/api/pay", window)

    # Give it time to activate AND deactivate.
    await asyncio.sleep(0.3)

    state = await engine.backend.get_state("/api/pay")
    assert state.status == RouteStatus.ACTIVE


# ---------------------------------------------------------------------------
# engine.schedule_maintenance() delegates to scheduler
# ---------------------------------------------------------------------------


async def test_engine_schedule_maintenance(engine):
    await engine.register("/api/pay", {"status": "active"})
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        reason="via engine",
    )
    await engine.schedule_maintenance("/api/pay", window)

    # State is persisted immediately.
    state = await engine.backend.get_state("/api/pay")
    assert state.status == RouteStatus.MAINTENANCE

    # Task is running.
    scheduled = await engine.scheduler.list_scheduled()
    assert any(p == "/api/pay" for p, _ in scheduled)

    # Cleanup.
    await engine.scheduler.cancel("/api/pay")


# ---------------------------------------------------------------------------
# restore_from_backend()
# ---------------------------------------------------------------------------


async def test_restore_from_backend_schedules_future_windows(engine):
    """restore_from_backend re-schedules windows that haven't expired."""
    await engine.register("/api/pay", {"status": "active"})
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        reason="to restore",
    )
    # Persist window in backend as if a previous server run had set it.
    await engine.set_maintenance("/api/pay", reason="to restore", window=window)

    # New scheduler instance simulating a fresh server start.
    new_scheduler = MaintenanceScheduler(engine=engine)
    await new_scheduler.restore_from_backend()

    scheduled = await new_scheduler.list_scheduled()
    assert any(p == "/api/pay" for p, _ in scheduled)

    await new_scheduler.cancel("/api/pay")


async def test_restore_skips_expired_windows(engine):
    """restore_from_backend ignores windows whose end time is in the past."""
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),  # already expired
        reason="expired",
    )
    from waygate.core.models import RouteState, RouteStatus

    await engine.backend.set_state(
        "/api/old",
        RouteState(
            path="/api/old",
            status=RouteStatus.MAINTENANCE,
            window=window,
        ),
    )

    new_scheduler = MaintenanceScheduler(engine=engine)
    await new_scheduler.restore_from_backend()

    scheduled = await new_scheduler.list_scheduled()
    assert not any(p == "/api/old" for p, _ in scheduled)
