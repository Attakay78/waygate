"""Tests for waygate.core.feature_flags.models.

All tests are sync and pure — no I/O, no async, no openfeature dependency.
Models are importable without the [flags] extra.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from waygate.core.feature_flags.models import (
    EvaluationContext,
    EvaluationReason,
    FeatureFlag,
    FlagStatus,
    FlagType,
    FlagVariation,
    Operator,
    Prerequisite,
    ResolutionDetails,
    RolloutVariation,
    RuleClause,
    ScheduledChange,
    ScheduledChangeAction,
    Segment,
    SegmentRule,
    TargetingRule,
)

# ── FlagType ─────────────────────────────────────────────────────────────────


class TestFlagType:
    def test_all_values(self):
        assert FlagType.BOOLEAN == "boolean"
        assert FlagType.STRING == "string"
        assert FlagType.INTEGER == "integer"
        assert FlagType.FLOAT == "float"
        assert FlagType.JSON == "json"


# ── FlagVariation ─────────────────────────────────────────────────────────────


class TestFlagVariation:
    def test_boolean_variation(self):
        v = FlagVariation(name="on", value=True)
        assert v.name == "on"
        assert v.value is True
        assert v.description == ""

    def test_string_variation(self):
        v = FlagVariation(name="blue", value="blue", description="Blue variant")
        assert v.value == "blue"
        assert v.description == "Blue variant"

    def test_json_variation(self):
        v = FlagVariation(name="config", value={"limit": 100, "burst": 20})
        assert v.value == {"limit": 100, "burst": 20}

    def test_list_variation(self):
        v = FlagVariation(name="tags", value=["a", "b"])
        assert v.value == ["a", "b"]


# ── RolloutVariation ──────────────────────────────────────────────────────────


class TestRolloutVariation:
    def test_valid(self):
        rv = RolloutVariation(variation="on", weight=25_000)
        assert rv.variation == "on"
        assert rv.weight == 25_000

    def test_weight_zero(self):
        rv = RolloutVariation(variation="off", weight=0)
        assert rv.weight == 0

    def test_weight_max(self):
        rv = RolloutVariation(variation="on", weight=100_000)
        assert rv.weight == 100_000

    def test_weight_over_max_rejected(self):
        with pytest.raises(ValidationError):
            RolloutVariation(variation="on", weight=100_001)

    def test_weight_negative_rejected(self):
        with pytest.raises(ValidationError):
            RolloutVariation(variation="on", weight=-1)


# ── Operator ──────────────────────────────────────────────────────────────────


class TestOperator:
    def test_all_operators_present(self):
        expected = {
            "is",
            "is_not",
            "contains",
            "not_contains",
            "starts_with",
            "ends_with",
            "matches",
            "not_matches",
            "gt",
            "gte",
            "lt",
            "lte",
            "before",
            "after",
            "in",
            "not_in",
            "in_segment",
            "not_in_segment",
            "semver_eq",
            "semver_lt",
            "semver_gt",
        }
        actual = {op.value for op in Operator}
        assert actual == expected


# ── RuleClause ────────────────────────────────────────────────────────────────


class TestRuleClause:
    def test_basic(self):
        clause = RuleClause(attribute="role", operator=Operator.IS, values=["admin"])
        assert clause.attribute == "role"
        assert clause.operator == Operator.IS
        assert clause.values == ["admin"]
        assert clause.negate is False

    def test_negated(self):
        clause = RuleClause(attribute="plan", operator=Operator.IN, values=["free"], negate=True)
        assert clause.negate is True

    def test_multiple_values(self):
        clause = RuleClause(
            attribute="role", operator=Operator.IN, values=["admin", "moderator", "staff"]
        )
        assert len(clause.values) == 3


# ── TargetingRule ─────────────────────────────────────────────────────────────


class TestTargetingRule:
    def test_auto_id(self):
        rule = TargetingRule()
        assert len(rule.id) == 36  # UUID4 format

    def test_with_fixed_variation(self):
        rule = TargetingRule(
            clauses=[RuleClause(attribute="role", operator=Operator.IS, values=["admin"])],
            variation="on",
        )
        assert rule.variation == "on"
        assert rule.rollout is None

    def test_with_rollout(self):
        rule = TargetingRule(
            clauses=[],
            rollout=[
                RolloutVariation(variation="on", weight=50_000),
                RolloutVariation(variation="off", weight=50_000),
            ],
        )
        assert rule.variation is None
        assert len(rule.rollout) == 2

    def test_custom_id(self):
        rule = TargetingRule(id="my-rule-id")
        assert rule.id == "my-rule-id"


# ── Prerequisite ──────────────────────────────────────────────────────────────


class TestPrerequisite:
    def test_basic(self):
        prereq = Prerequisite(flag_key="auth_v2", variation="on")
        assert prereq.flag_key == "auth_v2"
        assert prereq.variation == "on"


# ── Segment ───────────────────────────────────────────────────────────────────


class TestSegment:
    def test_minimal(self):
        seg = Segment(key="beta", name="Beta Users")
        assert seg.key == "beta"
        assert seg.included == []
        assert seg.excluded == []
        assert seg.rules == []

    def test_with_members(self):
        seg = Segment(
            key="beta",
            name="Beta",
            included=["user_1", "user_2"],
            excluded=["user_99"],
        )
        assert "user_1" in seg.included
        assert "user_99" in seg.excluded

    def test_with_rules(self):
        rule = SegmentRule(
            clauses=[RuleClause(attribute="plan", operator=Operator.IN, values=["pro"])]
        )
        seg = Segment(key="pro_users", name="Pro Users", rules=[rule])
        assert len(seg.rules) == 1


# ── ScheduledChange ───────────────────────────────────────────────────────────


class TestScheduledChange:
    def test_auto_id(self):
        from datetime import datetime

        sc = ScheduledChange(
            execute_at=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            action=ScheduledChangeAction.ENABLE,
        )
        assert len(sc.id) == 36
        assert sc.action == ScheduledChangeAction.ENABLE
        assert sc.created_by == "system"

    def test_all_actions(self):
        assert ScheduledChangeAction.ENABLE == "enable"
        assert ScheduledChangeAction.DISABLE == "disable"
        assert ScheduledChangeAction.UPDATE_ROLLOUT == "update_rollout"
        assert ScheduledChangeAction.ADD_RULE == "add_rule"
        assert ScheduledChangeAction.DELETE_RULE == "delete_rule"


# ── FeatureFlag ───────────────────────────────────────────────────────────────


def _make_boolean_flag(key: str = "my_flag", enabled: bool = True) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="My Flag",
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough="off",
        enabled=enabled,
    )


class TestFeatureFlag:
    def test_minimal_boolean_flag(self):
        flag = _make_boolean_flag()
        assert flag.key == "my_flag"
        assert flag.type == FlagType.BOOLEAN
        assert flag.enabled is True
        assert flag.status == FlagStatus.ACTIVE
        assert flag.temporary is True

    def test_get_variation_value_found(self):
        flag = _make_boolean_flag()
        assert flag.get_variation_value("on") is True
        assert flag.get_variation_value("off") is False

    def test_get_variation_value_missing(self):
        flag = _make_boolean_flag()
        assert flag.get_variation_value("nonexistent") is None

    def test_variation_names(self):
        flag = _make_boolean_flag()
        assert flag.variation_names() == ["on", "off"]

    def test_with_rollout_fallthrough(self):
        flag = FeatureFlag(
            key="rollout_flag",
            name="Rollout",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough=[
                RolloutVariation(variation="on", weight=25_000),
                RolloutVariation(variation="off", weight=75_000),
            ],
        )
        assert isinstance(flag.fallthrough, list)
        assert len(flag.fallthrough) == 2

    def test_with_prerequisites(self):
        flag = _make_boolean_flag()
        flag.prerequisites = [Prerequisite(flag_key="auth_v2", variation="on")]
        assert len(flag.prerequisites) == 1

    def test_with_targets(self):
        flag = _make_boolean_flag()
        flag.targets = {"on": ["user_1", "user_2"]}
        assert "user_1" in flag.targets["on"]

    def test_disabled_flag(self):
        flag = _make_boolean_flag(enabled=False)
        assert flag.enabled is False


# ── EvaluationContext ─────────────────────────────────────────────────────────


class TestEvaluationContext:
    def test_minimal(self):
        ctx = EvaluationContext(key="user_123")
        assert ctx.key == "user_123"
        assert ctx.kind == "user"
        assert ctx.attributes == {}

    def test_all_named_fields(self):
        ctx = EvaluationContext(
            key="user_1",
            kind="user",
            email="user@example.com",
            ip="1.2.3.4",
            country="US",
            app_version="2.3.1",
        )
        assert ctx.email == "user@example.com"
        assert ctx.ip == "1.2.3.4"
        assert ctx.country == "US"
        assert ctx.app_version == "2.3.1"

    def test_all_attributes_merges_fields(self):
        ctx = EvaluationContext(
            key="user_1",
            kind="user",
            email="a@b.com",
            country="UK",
            attributes={"plan": "pro", "role": "admin"},
        )
        attrs = ctx.all_attributes()
        assert attrs["key"] == "user_1"
        assert attrs["kind"] == "user"
        assert attrs["email"] == "a@b.com"
        assert attrs["country"] == "UK"
        assert attrs["plan"] == "pro"
        assert attrs["role"] == "admin"

    def test_attributes_override_named_fields(self):
        """attributes dict wins over named fields when keys collide."""
        ctx = EvaluationContext(
            key="user_1",
            country="US",
            attributes={"country": "UK"},  # should win
        )
        attrs = ctx.all_attributes()
        assert attrs["country"] == "UK"

    def test_none_named_fields_excluded(self):
        ctx = EvaluationContext(key="user_1")
        attrs = ctx.all_attributes()
        assert "email" not in attrs
        assert "ip" not in attrs
        assert "country" not in attrs
        assert "app_version" not in attrs

    def test_custom_kind(self):
        ctx = EvaluationContext(key="org_42", kind="organization")
        assert ctx.kind == "organization"
        assert ctx.all_attributes()["kind"] == "organization"


# ── ResolutionDetails ─────────────────────────────────────────────────────────


class TestResolutionDetails:
    def test_rule_match(self):
        r = ResolutionDetails(
            value=True,
            variation="on",
            reason=EvaluationReason.RULE_MATCH,
            rule_id="rule_abc",
        )
        assert r.value is True
        assert r.variation == "on"
        assert r.reason == EvaluationReason.RULE_MATCH
        assert r.rule_id == "rule_abc"
        assert r.prerequisite_key is None

    def test_prerequisite_fail(self):
        r = ResolutionDetails(
            value=False,
            variation="off",
            reason=EvaluationReason.PREREQUISITE_FAIL,
            prerequisite_key="auth_v2",
        )
        assert r.prerequisite_key == "auth_v2"

    def test_error(self):
        r = ResolutionDetails(
            value=False,
            reason=EvaluationReason.ERROR,
            error_message="Provider timeout",
        )
        assert r.error_message == "Provider timeout"
        assert r.variation is None

    def test_all_reasons_valid(self):
        reasons = list(EvaluationReason)
        assert len(reasons) == 7
        assert EvaluationReason.OFF in reasons
        assert EvaluationReason.FALLTHROUGH in reasons
        assert EvaluationReason.TARGET_MATCH in reasons
        assert EvaluationReason.RULE_MATCH in reasons
        assert EvaluationReason.PREREQUISITE_FAIL in reasons
        assert EvaluationReason.ERROR in reasons
        assert EvaluationReason.DEFAULT in reasons
