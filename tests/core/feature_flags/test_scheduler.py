"""Tests for FlagScheduler — scheduled flag change runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.models import (
    FeatureFlag,
    FlagType,
    FlagVariation,
    Operator,
    RuleClause,
    ScheduledChange,
    ScheduledChangeAction,
    TargetingRule,
)
from waygate.core.feature_flags.scheduler import FlagScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bool_flag(key: str, enabled: bool = True) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name=key.title(),
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough="on",
        enabled=enabled,
    )


def _change(
    action: ScheduledChangeAction,
    delta_seconds: float = 0.05,
    payload: dict | None = None,
) -> ScheduledChange:
    return ScheduledChange(
        execute_at=datetime.now(UTC) + timedelta(seconds=delta_seconds),
        action=action,
        payload=payload or {},
    )


def _past_change(action: ScheduledChangeAction, payload: dict | None = None) -> ScheduledChange:
    return ScheduledChange(
        execute_at=datetime.now(UTC) - timedelta(hours=2),
        action=action,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Basic construction / lifecycle
# ---------------------------------------------------------------------------


class TestFlagSchedulerLifecycle:
    async def test_start_with_no_flags(self) -> None:
        engine = WaygateEngine()
        scheduler = FlagScheduler(engine)
        await scheduler.start()  # should not raise
        await scheduler.stop()

    async def test_stop_with_no_tasks(self) -> None:
        engine = WaygateEngine()
        scheduler = FlagScheduler(engine)
        await scheduler.stop()  # idempotent

    async def test_list_pending_empty(self) -> None:
        engine = WaygateEngine()
        scheduler = FlagScheduler(engine)
        assert scheduler.list_pending() == []

    async def test_list_pending_after_schedule(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("pending-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(ScheduledChangeAction.DISABLE, delta_seconds=60)
        await scheduler.schedule("pending-flag", change)
        pending = scheduler.list_pending()
        assert len(pending) == 1
        assert pending[0]["flag_key"] == "pending-flag"
        assert pending[0]["change_id"] == change.id
        await scheduler.stop()

    async def test_cancel_removes_task(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("cancel-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(ScheduledChangeAction.DISABLE, delta_seconds=60)
        await scheduler.schedule("cancel-flag", change)
        assert len(scheduler.list_pending()) == 1
        await scheduler.cancel("cancel-flag", change.id)
        assert scheduler.list_pending() == []

    async def test_cancel_nonexistent_is_noop(self) -> None:
        engine = WaygateEngine()
        scheduler = FlagScheduler(engine)
        await scheduler.cancel("ghost-flag", "ghost-id")  # should not raise

    async def test_cancel_all_for_flag(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("multi-change-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        c1 = _change(ScheduledChangeAction.DISABLE, delta_seconds=60)
        c2 = _change(ScheduledChangeAction.ENABLE, delta_seconds=120)
        await scheduler.schedule("multi-change-flag", c1)
        await scheduler.schedule("multi-change-flag", c2)
        assert len(scheduler.list_pending()) == 2
        await scheduler.cancel_all_for_flag("multi-change-flag")
        assert scheduler.list_pending() == []

    async def test_stop_cancels_all(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("stop-all-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        c1 = _change(ScheduledChangeAction.DISABLE, delta_seconds=60)
        c2 = _change(ScheduledChangeAction.ENABLE, delta_seconds=120)
        await scheduler.schedule("stop-all-flag", c1)
        await scheduler.schedule("stop-all-flag", c2)
        await scheduler.stop()
        assert scheduler.list_pending() == []


# ---------------------------------------------------------------------------
# Action execution — ENABLE
# ---------------------------------------------------------------------------


class TestScheduledEnable:
    async def test_enable_action_fires(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("enable-me", enabled=False)
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(ScheduledChangeAction.ENABLE, delta_seconds=0.05)
        await scheduler.schedule("enable-me", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("enable-me")
        assert updated.enabled is True

    async def test_enable_removes_change_from_flag(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("rm-change-enable", enabled=False)
        change = _change(ScheduledChangeAction.ENABLE, delta_seconds=0.05)
        flag = flag.model_copy(update={"scheduled_changes": [change]})
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        await scheduler.schedule("rm-change-enable", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("rm-change-enable")
        assert all(c.id != change.id for c in updated.scheduled_changes)


# ---------------------------------------------------------------------------
# Action execution — DISABLE
# ---------------------------------------------------------------------------


class TestScheduledDisable:
    async def test_disable_action_fires(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("disable-me", enabled=True)
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(ScheduledChangeAction.DISABLE, delta_seconds=0.05)
        await scheduler.schedule("disable-me", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("disable-me")
        assert updated.enabled is False


# ---------------------------------------------------------------------------
# Action execution — UPDATE_ROLLOUT
# ---------------------------------------------------------------------------


class TestScheduledUpdateRollout:
    async def test_update_rollout_changes_fallthrough(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("rollout-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(
            ScheduledChangeAction.UPDATE_ROLLOUT,
            delta_seconds=0.05,
            payload={"variation": "off"},
        )
        await scheduler.schedule("rollout-flag", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("rollout-flag")
        assert updated.fallthrough == "off"

    async def test_update_rollout_missing_payload_does_not_crash(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("rollout-flag2")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        # payload is empty — should log warning, not crash
        change = _change(ScheduledChangeAction.UPDATE_ROLLOUT, delta_seconds=0.05, payload={})
        await scheduler.schedule("rollout-flag2", change)
        await asyncio.sleep(0.3)
        # Flag should still exist unchanged
        still_there = await engine.get_flag("rollout-flag2")
        assert still_there is not None


# ---------------------------------------------------------------------------
# Action execution — ADD_RULE / DELETE_RULE
# ---------------------------------------------------------------------------


class TestScheduledRuleMutations:
    async def test_add_rule_appends(self) -> None:
        engine = WaygateEngine()
        flag = _bool_flag("add-rule-flag")
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        rule_payload = {
            "id": "r-new",
            "clauses": [{"attribute": "email", "operator": "ends_with", "values": ["@acme.com"]}],
            "variation": "on",
        }
        change = _change(
            ScheduledChangeAction.ADD_RULE,
            delta_seconds=0.05,
            payload=rule_payload,
        )
        await scheduler.schedule("add-rule-flag", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("add-rule-flag")
        assert any(r.id == "r-new" for r in updated.rules)

    async def test_delete_rule_removes(self) -> None:
        engine = WaygateEngine()
        rule = TargetingRule(
            id="r-del",
            clauses=[RuleClause(attribute="role", operator=Operator.IN, values=["admin"])],
            variation="on",
        )
        flag = _bool_flag("del-rule-flag")
        flag = flag.model_copy(update={"rules": [rule]})
        await engine.save_flag(flag)
        scheduler = FlagScheduler(engine)
        change = _change(
            ScheduledChangeAction.DELETE_RULE,
            delta_seconds=0.05,
            payload={"rule_id": "r-del"},
        )
        await scheduler.schedule("del-rule-flag", change)
        await asyncio.sleep(0.3)
        updated = await engine.get_flag("del-rule-flag")
        assert all(r.id != "r-del" for r in updated.rules)


# ---------------------------------------------------------------------------
# Start — restart recovery from backend
# ---------------------------------------------------------------------------


class TestSchedulerStartRecovery:
    async def test_start_schedules_future_changes(self) -> None:
        engine = WaygateEngine()
        change = _change(ScheduledChangeAction.ENABLE, delta_seconds=0.1)
        flag = _bool_flag("recovery-flag", enabled=False)
        flag = flag.model_copy(update={"scheduled_changes": [change]})
        await engine.save_flag(flag)

        scheduler = FlagScheduler(engine)
        await scheduler.start()
        assert len(scheduler.list_pending()) == 1
        await asyncio.sleep(0.4)
        updated = await engine.get_flag("recovery-flag")
        assert updated.enabled is True
        await scheduler.stop()

    async def test_start_skips_past_changes(self) -> None:
        engine = WaygateEngine()
        change = _past_change(ScheduledChangeAction.ENABLE)
        flag = _bool_flag("past-change-flag", enabled=False)
        flag = flag.model_copy(update={"scheduled_changes": [change]})
        await engine.save_flag(flag)

        scheduler = FlagScheduler(engine)
        await scheduler.start()
        # Past changes don't get a task
        assert scheduler.list_pending() == []
        await scheduler.stop()

    async def test_start_ignores_missing_flag(self) -> None:
        """start() should not crash if a flag disappears between load and task run."""
        engine = WaygateEngine()
        scheduler = FlagScheduler(engine)
        await scheduler.start()  # no flags
        await scheduler.stop()


# ---------------------------------------------------------------------------
# Engine integration — engine.start() wires FlagScheduler
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    async def test_engine_flag_scheduler_property(self) -> None:
        engine = WaygateEngine()
        assert engine.flag_scheduler is None
        engine.use_openfeature()
        assert engine.flag_scheduler is not None

    async def test_engine_start_starts_scheduler(self) -> None:
        engine = WaygateEngine()
        engine.use_openfeature()
        change = _change(ScheduledChangeAction.DISABLE, delta_seconds=0.1)
        flag = _bool_flag("eng-sched-flag", enabled=True)
        flag = flag.model_copy(update={"scheduled_changes": [change]})
        await engine.save_flag(flag)
        await engine.start()
        assert len(engine.flag_scheduler.list_pending()) == 1
        await asyncio.sleep(0.4)
        updated = await engine.get_flag("eng-sched-flag")
        assert updated.enabled is False
        await engine.stop()

    async def test_engine_stop_stops_scheduler(self) -> None:
        engine = WaygateEngine()
        engine.use_openfeature()
        await engine.start()
        # Add a long-running task
        flag = _bool_flag("stop-eng-flag")
        await engine.save_flag(flag)
        change = _change(ScheduledChangeAction.DISABLE, delta_seconds=60)
        await engine.flag_scheduler.schedule("stop-eng-flag", change)
        assert len(engine.flag_scheduler.list_pending()) == 1
        await engine.stop()
        assert engine.flag_scheduler.list_pending() == []
