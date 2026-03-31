"""FlagScheduler — asyncio.Task-based scheduled flag change runner.

Each :class:`ScheduledChange` on a :class:`FeatureFlag` gets one asyncio
task that sleeps until ``execute_at``, applies the action to the flag, then
removes the change from the flag's ``scheduled_changes`` list.

On startup the scheduler scans all flags and re-creates tasks for any
pending changes whose ``execute_at`` is still in the future (restart
recovery).

Supported :class:`~waygate.core.feature_flags.models.ScheduledChangeAction`\\ s:

* ``ENABLE`` — sets ``flag.enabled = True``
* ``DISABLE`` — sets ``flag.enabled = False``
* ``UPDATE_ROLLOUT`` — replaces ``flag.fallthrough`` with a new variation
  name or rollout list from ``payload``
* ``ADD_RULE`` — appends a :class:`TargetingRule` parsed from ``payload``
* ``DELETE_RULE`` — removes the rule with ``payload["rule_id"]``
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from waygate.core.engine import WaygateEngine

logger = logging.getLogger(__name__)


class FlagScheduler:
    """Manages scheduled flag changes using ``asyncio.Task`` objects.

    Parameters
    ----------
    engine:
        The :class:`~waygate.core.engine.WaygateEngine` used to read and
        write flags.
    """

    def __init__(self, engine: WaygateEngine) -> None:
        self._engine = engine
        # (flag_key, change_id) → running task
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Restore pending scheduled changes from all flags.

        Called by ``WaygateEngine.start()`` when feature flags are enabled.
        """
        try:
            flags = await self._engine.list_flags()
        except Exception:
            logger.exception("FlagScheduler: failed to load flags on startup")
            return

        now = datetime.now(UTC)
        count = 0
        for flag in flags:
            for change in list(flag.scheduled_changes):
                execute_at = change.execute_at
                if execute_at.tzinfo is None:
                    execute_at = execute_at.replace(tzinfo=UTC)
                if execute_at > now:
                    self._create_task(flag.key, change)
                    count += 1
        if count:
            logger.info("FlagScheduler: restored %d pending scheduled change(s)", count)

    async def stop(self) -> None:
        """Cancel all pending scheduled change tasks."""
        for task in list(self._tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def schedule(self, flag_key: str, change: Any) -> None:
        """Register a new scheduled change task.

        If a task already exists for ``(flag_key, change.id)`` it is
        cancelled and replaced.

        Parameters
        ----------
        flag_key:
            Key of the flag that owns the change.
        change:
            A :class:`~waygate.core.feature_flags.models.ScheduledChange`
            instance already appended to the flag's ``scheduled_changes``
            list and persisted to the backend.
        """
        await self.cancel(flag_key, change.id)
        self._create_task(flag_key, change)

    async def cancel(self, flag_key: str, change_id: str) -> None:
        """Cancel the task for a specific scheduled change, if any."""
        task = self._tasks.pop((flag_key, change_id), None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def cancel_all_for_flag(self, flag_key: str) -> None:
        """Cancel all pending tasks for *flag_key* (e.g. when a flag is deleted)."""
        keys_to_cancel = [k for k in self._tasks if k[0] == flag_key]
        for k in keys_to_cancel:
            task = self._tasks.pop(k)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def list_pending(self) -> list[dict[str, str]]:
        """Return a list of ``{"flag_key": ..., "change_id": ...}`` dicts."""
        return [{"flag_key": fk, "change_id": cid} for fk, cid in self._tasks]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_task(self, flag_key: str, change: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._run_change(flag_key, change),
            name=f"waygate-flag-scheduler:{flag_key}:{change.id}",
        )
        self._tasks[(flag_key, change.id)] = task
        task.add_done_callback(lambda t: self._tasks.pop((flag_key, change.id), None))
        return task

    async def _run_change(self, flag_key: str, change: Any) -> None:
        """Sleep until ``execute_at``, then apply the change to the flag."""
        execute_at = change.execute_at
        if execute_at.tzinfo is None:
            execute_at = execute_at.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        delay = (execute_at - now).total_seconds()
        if delay > 0:
            try:
                await anyio.sleep(delay)
            except asyncio.CancelledError:
                return

        logger.info(
            "FlagScheduler: executing change %s (action=%s) on flag %r",
            change.id,
            change.action,
            flag_key,
        )
        try:
            await self._apply_change(flag_key, change)
        except Exception:
            logger.exception(
                "FlagScheduler: error applying change %s on flag %r", change.id, flag_key
            )

    async def _apply_change(self, flag_key: str, change: Any) -> None:
        """Load the flag, mutate it, remove the change, and persist."""
        from waygate.core.feature_flags.models import ScheduledChangeAction, TargetingRule

        flag = await self._engine.get_flag(flag_key)
        if flag is None:
            logger.warning(
                "FlagScheduler: flag %r not found when applying change %s — skipping",
                flag_key,
                change.id,
            )
            return

        action = change.action
        payload = change.payload or {}

        if action == ScheduledChangeAction.ENABLE:
            flag = flag.model_copy(update={"enabled": True})
        elif action == ScheduledChangeAction.DISABLE:
            flag = flag.model_copy(update={"enabled": False})
        elif action == ScheduledChangeAction.UPDATE_ROLLOUT:
            new_fallthrough = payload.get("variation") or payload.get("rollout")
            if new_fallthrough is not None:
                flag = flag.model_copy(update={"fallthrough": new_fallthrough})
            else:
                logger.warning(
                    "FlagScheduler: UPDATE_ROLLOUT payload missing 'variation' for flag %r",
                    flag_key,
                )
        elif action == ScheduledChangeAction.ADD_RULE:
            try:
                new_rule = TargetingRule.model_validate(payload)
                updated_rules = list(flag.rules) + [new_rule]
                flag = flag.model_copy(update={"rules": updated_rules})
            except Exception as exc:
                logger.error(
                    "FlagScheduler: ADD_RULE payload invalid for flag %r: %s", flag_key, exc
                )
                return
        elif action == ScheduledChangeAction.DELETE_RULE:
            rule_id = payload.get("rule_id")
            updated_rules = [r for r in flag.rules if r.id != rule_id]
            flag = flag.model_copy(update={"rules": updated_rules})
        else:
            logger.warning("FlagScheduler: unknown action %r for flag %r", action, flag_key)
            return

        # Remove the executed change from the flag's scheduled_changes list.
        remaining = [c for c in flag.scheduled_changes if c.id != change.id]
        flag = flag.model_copy(update={"scheduled_changes": remaining})
        await self._engine.save_flag(flag)

        logger.info(
            "FlagScheduler: applied %s to flag %r (change %s)",
            action,
            flag_key,
            change.id,
        )
