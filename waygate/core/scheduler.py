"""MaintenanceScheduler — asyncio.Task based maintenance window runner.

Schedules future maintenance windows without Celery or APScheduler.
Each window gets one asyncio.Task that sleeps until ``start``, activates
maintenance, sleeps until ``end``, then re-enables the route.

On startup the scheduler restores any windows whose ``start`` is still in
the future that are stored in the backend (restart recovery).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio

from waygate.core.models import MaintenanceWindow

if TYPE_CHECKING:
    from waygate.core.engine import WaygateEngine

logger = logging.getLogger(__name__)


class MaintenanceScheduler:
    """Manages scheduled maintenance windows using ``asyncio.Task`` objects.

    Parameters
    ----------
    engine:
        The ``WaygateEngine`` instance used to activate and deactivate
        maintenance mode.  Passed as a forward reference to avoid a
        circular import at module level.
    """

    def __init__(self, engine: WaygateEngine) -> None:
        self._engine = engine
        # path → running asyncio.Task
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # path → window (for list_scheduled / restart recovery)
        self._windows: dict[str, MaintenanceWindow] = {}
        # Background polling task (picks up windows created by external
        # processes like the CLI after the server has already started).
        self._poll_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def schedule(
        self, path: str, window: MaintenanceWindow, actor: str = "scheduler"
    ) -> None:
        """Schedule a maintenance window for *path*.

        If a window already exists for *path* it is cancelled and replaced.

        Parameters
        ----------
        path:
            The route path to manage.
        window:
            The ``MaintenanceWindow`` with ``start`` and ``end`` datetimes.
        actor:
            Kept for API compatibility but not used by the automated
            activation/deactivation steps, which always record
            ``"scheduler"`` as their actor.  The user's actor is already
            captured by ``engine.schedule_maintenance()`` when the window
            is first persisted.
        """
        await self.cancel(path)

        self._windows[path] = window
        task = asyncio.create_task(
            self._run_window(path, window),
            name=f"waygate-scheduler:{path}",
        )
        self._tasks[path] = task
        task.add_done_callback(lambda t: self._on_task_done(path, t))
        logger.info(
            "waygate: scheduled maintenance for %r from %s to %s",
            path,
            window.start.isoformat(),
            window.end.isoformat(),
        )

    async def cancel(self, path: str) -> None:
        """Cancel a pending or running maintenance window for *path*.

        No-op if no window is scheduled for *path*.
        """
        task = self._tasks.pop(path, None)
        self._windows.pop(path, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def list_scheduled(self) -> list[tuple[str, MaintenanceWindow]]:
        """Return all currently scheduled (not yet finished) windows."""
        return [
            (path, window)
            for path, window in self._windows.items()
            if path in self._tasks and not self._tasks[path].done()
        ]

    async def restore_from_backend(self, actor: str = "scheduler") -> None:
        """Re-schedule any future maintenance windows stored in the backend.

        Called once at startup for restart recovery, and periodically by the
        polling loop to pick up windows written by external processes (CLI).

        Windows whose asyncio task is still alive are skipped — only truly
        new or newly-discovered windows from the backend are scheduled.
        """
        try:
            states = await self._engine.backend.list_states()
        except Exception:
            logger.exception("waygate: failed to restore scheduled windows from backend")
            return

        now = datetime.now(UTC)
        for state in states:
            if state.window is None:
                continue
            if state.window.end <= now:
                # Window already expired — skip.
                continue
            # Skip paths whose task is already running in this process.
            existing = self._tasks.get(state.path)
            if existing is not None and not existing.done():
                continue
            logger.info("waygate: restoring scheduled window for %r from backend", state.path)
            await self.schedule(state.path, state.window, actor=actor)

    def start_polling(self, interval_seconds: float = 30.0) -> None:
        """Start a background loop that polls the backend for new windows.

        This lets the server pick up maintenance windows created by external
        processes (e.g. the CLI) after the server has already started.
        The poll fires every *interval_seconds* (default: 30 s).

        Safe to call multiple times — if a poll loop is already running it is
        cancelled and replaced.
        """
        self.stop_polling()

        async def _poll() -> None:
            while True:
                await anyio.sleep(interval_seconds)
                try:
                    await self.restore_from_backend()
                except Exception:
                    logger.exception("waygate: scheduler poll cycle failed")

        self._poll_task = asyncio.create_task(_poll(), name="waygate-scheduler-poll")
        logger.info("waygate: scheduler polling started (interval=%.0fs)", interval_seconds)

    def stop_polling(self) -> None:
        """Cancel the background polling loop if it is running."""
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
            logger.info("waygate: scheduler polling stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_window(self, path: str, window: MaintenanceWindow) -> None:
        """Task body: sleep → activate → sleep → deactivate.

        Both the activation and deactivation audit entries always record
        ``"scheduler"`` as the actor to make it clear these are automated
        actions, not direct user operations.  The user who originally
        scheduled the window is already recorded in the audit log by
        ``engine.schedule_maintenance()``.
        """
        now = datetime.now(UTC)

        # Sleep until start (if start is still in the future).
        seconds_until_start = (window.start - now).total_seconds()
        if seconds_until_start > 0:
            await anyio.sleep(seconds_until_start)

        # Activate maintenance — always attributed to "scheduler".
        try:
            await self._engine.set_maintenance(
                path,
                reason=window.reason,
                window=window,
                actor="scheduler",
            )
            logger.info("waygate: maintenance window opened for %r", path)
        except Exception:
            logger.exception("waygate: failed to activate maintenance for %r", path)
            return

        # Sleep until end.
        now = datetime.now(UTC)
        seconds_until_end = (window.end - now).total_seconds()
        if seconds_until_end > 0:
            await anyio.sleep(seconds_until_end)

        # Deactivate maintenance — always attributed to "scheduler".
        try:
            await self._engine.enable(path, actor="scheduler")
            logger.info("waygate: maintenance window closed for %r", path)
        except Exception:
            logger.exception("waygate: failed to deactivate maintenance for %r", path)

    def _on_task_done(self, path: str, task: asyncio.Task[None]) -> None:
        """Callback: remove the task from the tracking dicts when it finishes."""
        self._tasks.pop(path, None)
        self._windows.pop(path, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "waygate: scheduler task for %r raised an exception: %s",
                path,
                task.exception(),
            )
