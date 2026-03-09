"""Tests for the scheduler polling loop that picks up windows written by the CLI.

Root cause being tested:
    ``shield schedule`` runs in a short-lived CLI process.  That process writes
    the window to the backend and creates an asyncio task — but the task is
    destroyed when the CLI exits.  The long-running server must pick up the
    window via the polling loop in ``MaintenanceScheduler``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import MaintenanceWindow, RouteState, RouteStatus


def _engine() -> ShieldEngine:
    return ShieldEngine(backend=MemoryBackend())


# ---------------------------------------------------------------------------
# restore_from_backend — idempotency: already-tracked tasks are not replaced
# ---------------------------------------------------------------------------


async def test_restore_skips_already_tracked_path():
    """restore_from_backend must not cancel a live task for a path it already
    knows about — calling it twice must not reset the window."""
    engine = _engine()

    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    window = MaintenanceWindow(start=start, end=end, reason="Planned")

    # Schedule via engine (creates task in this process).
    await engine.schedule_maintenance("GET:/orders", window)

    task_before = engine.scheduler._tasks.get("GET:/orders")
    assert task_before is not None

    # Simulate a second restore call (as if the polling loop fired).
    await engine.scheduler.restore_from_backend()

    # The task must be the SAME object — it was not cancelled and replaced.
    task_after = engine.scheduler._tasks.get("GET:/orders")
    assert task_after is task_before, (
        "restore_from_backend cancelled and replaced a live task"
    )

    task_before.cancel()


async def test_restore_picks_up_new_window_written_externally():
    """Simulate the CLI writing a window to the backend directly.  A subsequent
    restore_from_backend call must create a task in the running process."""
    engine = _engine()

    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    window = MaintenanceWindow(start=start, end=end, reason="External")

    # Write the state directly — simulates what the CLI does to the backend.
    await engine.backend.set_state(
        "GET:/products",
        RouteState(
            path="GET:/products",
            status=RouteStatus.MAINTENANCE,
            reason="External",
            window=window,
        ),
    )

    # Scheduler has no task for this path yet.
    assert "GET:/products" not in engine.scheduler._tasks

    # restore_from_backend should create the task.
    await engine.scheduler.restore_from_backend()

    assert "GET:/products" in engine.scheduler._tasks
    task = engine.scheduler._tasks["GET:/products"]
    assert not task.done()
    task.cancel()


async def test_restore_skips_expired_windows():
    """Windows whose end time is in the past must be ignored."""
    engine = _engine()

    start = datetime.now(UTC) - timedelta(hours=3)
    end = datetime.now(UTC) - timedelta(hours=1)  # already ended

    await engine.backend.set_state(
        "GET:/old",
        RouteState(
            path="GET:/old",
            status=RouteStatus.MAINTENANCE,
            window=MaintenanceWindow(start=start, end=end),
        ),
    )

    await engine.scheduler.restore_from_backend()

    assert "GET:/old" not in engine.scheduler._tasks


# ---------------------------------------------------------------------------
# start_polling / stop_polling
# ---------------------------------------------------------------------------


async def test_start_polling_creates_background_task():
    engine = _engine()
    engine.scheduler.start_polling(interval_seconds=60)

    assert engine.scheduler._poll_task is not None
    assert not engine.scheduler._poll_task.done()

    engine.scheduler.stop_polling()


async def test_stop_polling_cancels_task():
    engine = _engine()
    engine.scheduler.start_polling(interval_seconds=60)
    engine.scheduler.stop_polling()

    assert engine.scheduler._poll_task is None


async def test_start_polling_twice_replaces_old_task():
    engine = _engine()
    engine.scheduler.start_polling(interval_seconds=60)
    first_task = engine.scheduler._poll_task

    engine.scheduler.start_polling(interval_seconds=60)
    second_task = engine.scheduler._poll_task

    # Yield so the event loop can process the cancellation of first_task.
    await asyncio.sleep(0)

    assert second_task is not first_task
    # first_task should be done/cancelled after the yield.
    assert first_task is not None
    assert first_task.done() or first_task.cancelled()

    engine.scheduler.stop_polling()


async def test_polling_discovers_externally_written_window():
    """The polling loop must call restore_from_backend and pick up a window
    that was written to the backend AFTER the server started."""
    engine = _engine()

    # Start polling with a very short interval so the test doesn't wait long.
    engine.scheduler.start_polling(interval_seconds=0.1)

    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    window = MaintenanceWindow(start=start, end=end, reason="Poll test")

    await engine.backend.set_state(
        "GET:/api",
        RouteState(
            path="GET:/api",
            status=RouteStatus.MAINTENANCE,
            window=window,
        ),
    )

    # Wait for at least two poll cycles.
    await anyio.sleep(0.3)

    assert "GET:/api" in engine.scheduler._tasks, (
        "Scheduler did not pick up the externally-written window via polling"
    )

    engine.scheduler.stop_polling()
    engine.scheduler._tasks["GET:/api"].cancel()


# ---------------------------------------------------------------------------
# ShieldMiddleware lifespan wires up polling automatically
# ---------------------------------------------------------------------------


async def _lifespan_startup(app: Any) -> asyncio.Task[Any]:
    """Simulate ASGI lifespan startup and return after startup.complete fires.

    Returns the background lifespan task (caller must cancel it when done).
    """
    startup_complete: asyncio.Event = asyncio.Event()
    call_count = 0

    async def receive() -> dict[str, Any]:
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return {"type": "lifespan.startup"}
        # Block until cancelled — we never send a real shutdown.
        await asyncio.sleep(3600)
        return {}

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "lifespan.startup.complete":
            startup_complete.set()

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}
    task = asyncio.create_task(app(scope, receive, send))
    await startup_complete.wait()
    return task


async def test_lifespan_starts_polling():
    """ShieldMiddleware must start the polling loop on lifespan.startup.complete."""
    from fastapi import FastAPI

    from shield.fastapi.middleware import ShieldMiddleware

    engine = _engine()
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    task = await _lifespan_startup(app)

    try:
        assert engine.scheduler._poll_task is not None, (
            "start_polling() was not called during lifespan.startup.complete"
        )
        assert not engine.scheduler._poll_task.done()
    finally:
        engine.scheduler.stop_polling()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_lifespan_stops_polling_on_shutdown():
    """ShieldMiddleware must call stop_polling on lifespan.shutdown.complete."""
    from fastapi import FastAPI

    from shield.fastapi.middleware import ShieldMiddleware

    engine = _engine()
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)

    startup_done: asyncio.Event = asyncio.Event()
    # Separate gate — the test sets this AFTER checking the poll task so that
    # the shutdown message is not returned until the test is ready for it.
    allow_shutdown: asyncio.Event = asyncio.Event()
    shutdown_done: asyncio.Event = asyncio.Event()
    phase = [0]

    async def receive() -> dict[str, Any]:
        if phase[0] == 0:
            phase[0] = 1
            return {"type": "lifespan.startup"}
        # Block until the test explicitly allows the shutdown to proceed.
        await allow_shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message: dict[str, Any]) -> None:
        t = message.get("type")
        if t == "lifespan.startup.complete":
            startup_done.set()
        elif t == "lifespan.shutdown.complete":
            shutdown_done.set()

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}
    lifespan_task = asyncio.create_task(app(scope, receive, send))

    # Wait for startup then let the event loop settle.
    await startup_done.wait()
    await asyncio.sleep(0)

    assert engine.scheduler._poll_task is not None, (
        "start_polling() was not called during lifespan.startup.complete"
    )

    # Allow the lifespan to proceed to shutdown now that we have checked.
    allow_shutdown.set()
    await shutdown_done.wait()
    await lifespan_task

    assert engine.scheduler._poll_task is None, (
        "stop_polling() was not called during lifespan.shutdown.complete"
    )
