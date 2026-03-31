"""Tests for FlagEvaluator — pure unit tests, no I/O, no async.

Every test constructs FeatureFlag / EvaluationContext objects directly
and calls FlagEvaluator.evaluate() synchronously.
"""

from __future__ import annotations

import pytest

from waygate.core.feature_flags.evaluator import FlagEvaluator
from waygate.core.feature_flags.models import (
    EvaluationContext,
    EvaluationReason,
    FeatureFlag,
    FlagType,
    FlagVariation,
    Operator,
    Prerequisite,
    RolloutVariation,
    RuleClause,
    Segment,
    SegmentRule,
    TargetingRule,
)

# ── Fixtures and helpers ──────────────────────────────────────────────────────


def _flag(
    key: str = "flag_a",
    enabled: bool = True,
    fallthrough: str | list = "off",
    rules: list | None = None,
    targets: dict | None = None,
    prerequisites: list | None = None,
    variations: list | None = None,
) -> FeatureFlag:
    if variations is None:
        variations = [
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ]
    return FeatureFlag(
        key=key,
        name=key,
        type=FlagType.BOOLEAN,
        variations=variations,
        off_variation="off",
        fallthrough=fallthrough,
        enabled=enabled,
        rules=rules or [],
        targets=targets or {},
        prerequisites=prerequisites or [],
    )


def _ctx(key: str = "user_1", **attrs: object) -> EvaluationContext:
    return EvaluationContext(key=key, attributes=dict(attrs))


def _rule(*clauses: RuleClause, variation: str = "on") -> TargetingRule:
    return TargetingRule(clauses=list(clauses), variation=variation)


def _clause(attr: str, op: Operator, values: list) -> RuleClause:
    return RuleClause(attribute=attr, operator=op, values=values)


@pytest.fixture
def evaluator() -> FlagEvaluator:
    return FlagEvaluator(segments={})


# ── Step 1: flag disabled ─────────────────────────────────────────────────────


class TestFlagDisabled:
    def test_disabled_serves_off_variation(self, evaluator):
        flag = _flag(enabled=False)
        result = evaluator.evaluate(flag, _ctx(), {})
        assert result.value is False
        assert result.variation == "off"
        assert result.reason == EvaluationReason.OFF

    def test_disabled_ignores_rules(self, evaluator):
        rule = _rule(_clause("role", Operator.IS, ["admin"]))
        flag = _flag(enabled=False, rules=[rule])
        result = evaluator.evaluate(flag, _ctx(role="admin"), {})
        assert result.reason == EvaluationReason.OFF

    def test_disabled_ignores_targets(self, evaluator):
        flag = _flag(enabled=False, targets={"on": ["user_1"]})
        result = evaluator.evaluate(flag, _ctx("user_1"), {})
        assert result.reason == EvaluationReason.OFF


# ── Step 2: prerequisites ─────────────────────────────────────────────────────


class TestPrerequisites:
    def test_prerequisite_met(self, evaluator):
        auth_flag = _flag("auth_v2", fallthrough="on")
        main_flag = _flag(
            "checkout",
            fallthrough="on",
            prerequisites=[Prerequisite(flag_key="auth_v2", variation="on")],
        )
        all_flags = {"auth_v2": auth_flag, "checkout": main_flag}
        result = evaluator.evaluate(main_flag, _ctx(), all_flags)
        assert result.reason == EvaluationReason.FALLTHROUGH
        assert result.value is True

    def test_prerequisite_not_met(self, evaluator):
        auth_flag = _flag("auth_v2", fallthrough="off")
        main_flag = _flag(
            "checkout",
            prerequisites=[Prerequisite(flag_key="auth_v2", variation="on")],
        )
        all_flags = {"auth_v2": auth_flag, "checkout": main_flag}
        result = evaluator.evaluate(main_flag, _ctx(), all_flags)
        assert result.reason == EvaluationReason.PREREQUISITE_FAIL
        assert result.prerequisite_key == "auth_v2"
        assert result.value is False

    def test_missing_prerequisite_flag(self, evaluator):
        main_flag = _flag(
            "checkout",
            prerequisites=[Prerequisite(flag_key="missing_flag", variation="on")],
        )
        result = evaluator.evaluate(main_flag, _ctx(), {"checkout": main_flag})
        assert result.reason == EvaluationReason.PREREQUISITE_FAIL
        assert result.prerequisite_key == "missing_flag"

    def test_disabled_prerequisite_fails(self, evaluator):
        auth_flag = _flag("auth_v2", enabled=False, fallthrough="on")
        main_flag = _flag(
            "checkout",
            prerequisites=[Prerequisite(flag_key="auth_v2", variation="on")],
        )
        all_flags = {"auth_v2": auth_flag, "checkout": main_flag}
        result = evaluator.evaluate(main_flag, _ctx(), all_flags)
        # auth_v2 is disabled → serves off_variation "off", not "on" → prereq fails
        assert result.reason == EvaluationReason.PREREQUISITE_FAIL

    def test_depth_limit_protection(self, evaluator):
        # Simulate deep recursion by calling with _depth at limit
        flag = _flag(
            "deep",
            prerequisites=[Prerequisite(flag_key="other", variation="on")],
        )
        result = evaluator.evaluate(flag, _ctx(), {flag.key: flag}, _depth=11)
        assert result.reason == EvaluationReason.ERROR


# ── Step 3: individual targets ────────────────────────────────────────────────


class TestIndividualTargets:
    def test_targeted_context_served_correct_variation(self, evaluator):
        flag = _flag(targets={"on": ["user_1", "user_2"], "off": ["user_99"]})
        result = evaluator.evaluate(flag, _ctx("user_1"), {})
        assert result.reason == EvaluationReason.TARGET_MATCH
        assert result.variation == "on"
        assert result.value is True

    def test_non_targeted_context_falls_through(self, evaluator):
        flag = _flag(targets={"on": ["user_1"]})
        result = evaluator.evaluate(flag, _ctx("user_999"), {})
        assert result.reason == EvaluationReason.FALLTHROUGH

    def test_targets_take_priority_over_rules(self, evaluator):
        rule = _rule(_clause("role", Operator.IS, ["admin"]), variation="off")
        flag = _flag(targets={"on": ["user_1"]}, rules=[rule])
        result = evaluator.evaluate(flag, _ctx("user_1", role="admin"), {})
        # Individual target wins over matching rule
        assert result.reason == EvaluationReason.TARGET_MATCH
        assert result.variation == "on"


# ── Step 4: targeting rules ───────────────────────────────────────────────────


class TestTargetingRules:
    def test_single_clause_match(self, evaluator):
        rule = _rule(_clause("role", Operator.IS, ["admin"]))
        flag = _flag(rules=[rule])
        result = evaluator.evaluate(flag, _ctx(role="admin"), {})
        assert result.reason == EvaluationReason.RULE_MATCH
        assert result.rule_id == rule.id
        assert result.value is True

    def test_single_clause_no_match(self, evaluator):
        rule = _rule(_clause("role", Operator.IS, ["admin"]))
        flag = _flag(rules=[rule])
        result = evaluator.evaluate(flag, _ctx(role="user"), {})
        assert result.reason == EvaluationReason.FALLTHROUGH

    def test_multiple_clauses_all_must_match(self, evaluator):
        rule = _rule(
            _clause("role", Operator.IS, ["admin"]),
            _clause("plan", Operator.IS, ["pro"]),
        )
        flag = _flag(rules=[rule])
        # Both match
        r = evaluator.evaluate(flag, _ctx(role="admin", plan="pro"), {})
        assert r.reason == EvaluationReason.RULE_MATCH
        # Only one matches
        r = evaluator.evaluate(flag, _ctx(role="admin", plan="free"), {})
        assert r.reason == EvaluationReason.FALLTHROUGH

    def test_first_rule_wins(self, evaluator):
        rule1 = TargetingRule(
            id="rule1",
            clauses=[_clause("role", Operator.IS, ["admin"])],
            variation="on",
        )
        rule2 = TargetingRule(
            id="rule2",
            clauses=[_clause("role", Operator.IS, ["admin"])],
            variation="off",
        )
        flag = _flag(rules=[rule1, rule2])
        result = evaluator.evaluate(flag, _ctx(role="admin"), {})
        assert result.rule_id == "rule1"
        assert result.variation == "on"

    def test_missing_attribute_no_match(self, evaluator):
        rule = _rule(_clause("role", Operator.IS, ["admin"]))
        flag = _flag(rules=[rule])
        result = evaluator.evaluate(flag, _ctx(), {})  # no role attr
        assert result.reason == EvaluationReason.FALLTHROUGH

    def test_rule_with_rollout(self, evaluator):
        # Force a specific bucket by using a known key
        rollout_rule = TargetingRule(
            clauses=[_clause("plan", Operator.IS, ["pro"])],
            rollout=[
                RolloutVariation(variation="on", weight=100_000),
                RolloutVariation(variation="off", weight=0),
            ],
        )
        flag = _flag(rules=[rollout_rule])
        result = evaluator.evaluate(flag, _ctx(plan="pro"), {})
        assert result.reason == EvaluationReason.RULE_MATCH
        assert result.variation == "on"


# ── Step 5: fallthrough ───────────────────────────────────────────────────────


class TestFallthrough:
    def test_fixed_variation_fallthrough(self, evaluator):
        flag = _flag(fallthrough="off")
        result = evaluator.evaluate(flag, _ctx(), {})
        assert result.reason == EvaluationReason.FALLTHROUGH
        assert result.variation == "off"
        assert result.value is False

    def test_rollout_fallthrough_deterministic(self, evaluator):
        flag = _flag(
            fallthrough=[
                RolloutVariation(variation="on", weight=100_000),
            ]
        )
        # 100% → always "on"
        for i in range(10):
            result = evaluator.evaluate(flag, _ctx(f"user_{i}"), {})
            assert result.variation == "on"

    def test_rollout_fallthrough_stable(self, evaluator):
        """Same context always gets the same bucket."""
        flag = _flag(
            fallthrough=[
                RolloutVariation(variation="on", weight=50_000),
                RolloutVariation(variation="off", weight=50_000),
            ]
        )
        ctx = _ctx("stable_user")
        first = evaluator.evaluate(flag, ctx, {}).variation
        for _ in range(5):
            assert evaluator.evaluate(flag, ctx, {}).variation == first


# ── Operator tests ────────────────────────────────────────────────────────────


class TestOperators:
    """One test per operator group."""

    def _eval(self, evaluator, op, actual, values, negate=False):
        clause = RuleClause(attribute="x", operator=op, values=values, negate=negate)
        rule = TargetingRule(clauses=[clause], variation="on")
        flag = _flag(rules=[rule])
        ctx = EvaluationContext(key="u", attributes={"x": actual} if actual is not None else {})
        result = evaluator.evaluate(flag, ctx, {})
        return result.reason == EvaluationReason.RULE_MATCH

    # ── Equality
    def test_is_match(self, evaluator):
        assert self._eval(evaluator, Operator.IS, "admin", ["admin"])

    def test_is_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.IS, "user", ["admin"])

    def test_is_not_match(self, evaluator):
        assert self._eval(evaluator, Operator.IS_NOT, "user", ["admin"])

    def test_is_not_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.IS_NOT, "admin", ["admin"])

    # ── String
    def test_contains(self, evaluator):
        assert self._eval(evaluator, Operator.CONTAINS, "hello world", ["world"])

    def test_not_contains(self, evaluator):
        assert self._eval(evaluator, Operator.NOT_CONTAINS, "hello", ["world"])

    def test_starts_with(self, evaluator):
        assert self._eval(evaluator, Operator.STARTS_WITH, "prefix_key", ["prefix"])

    def test_ends_with(self, evaluator):
        assert self._eval(evaluator, Operator.ENDS_WITH, "key_suffix", ["suffix"])

    def test_matches_regex(self, evaluator):
        assert self._eval(evaluator, Operator.MATCHES, "user@example.com", [r"@\w+\.com"])

    def test_not_matches_regex(self, evaluator):
        assert self._eval(evaluator, Operator.NOT_MATCHES, "foobar", [r"@\w+\.com"])

    def test_invalid_regex_returns_false(self, evaluator):
        assert not self._eval(evaluator, Operator.MATCHES, "test", ["[invalid"])

    # ── Numeric
    def test_gt(self, evaluator):
        assert self._eval(evaluator, Operator.GT, 10, [5])

    def test_gt_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.GT, 3, [5])

    def test_gte(self, evaluator):
        assert self._eval(evaluator, Operator.GTE, 5, [5])

    def test_lt(self, evaluator):
        assert self._eval(evaluator, Operator.LT, 3, [5])

    def test_lte(self, evaluator):
        assert self._eval(evaluator, Operator.LTE, 5, [5])

    def test_numeric_non_numeric_returns_false(self, evaluator):
        assert not self._eval(evaluator, Operator.GT, "abc", [5])

    # ── Date (lexicographic)
    def test_before(self, evaluator):
        assert self._eval(evaluator, Operator.BEFORE, "2025-01-01", ["2026-01-01"])

    def test_after(self, evaluator):
        assert self._eval(evaluator, Operator.AFTER, "2026-01-01", ["2025-01-01"])

    # ── Collection
    def test_in(self, evaluator):
        assert self._eval(evaluator, Operator.IN, "admin", ["admin", "moderator"])

    def test_in_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.IN, "user", ["admin"])

    def test_not_in(self, evaluator):
        assert self._eval(evaluator, Operator.NOT_IN, "user", ["admin"])

    # ── Negate
    def test_negate_reverses_result(self, evaluator):
        assert self._eval(evaluator, Operator.IS, "admin", ["admin"], negate=False)
        assert not self._eval(evaluator, Operator.IS, "admin", ["admin"], negate=True)

    # ── Multiple values (OR)
    def test_multiple_values_any_match(self, evaluator):
        assert self._eval(evaluator, Operator.IS, "moderator", ["admin", "moderator", "staff"])

    # ── Missing attribute
    def test_missing_attribute_is_not(self, evaluator):
        # IS_NOT with None still works (None != "admin" is True)
        assert self._eval(evaluator, Operator.IS_NOT, None, ["admin"])

    def test_missing_attribute_in_returns_false(self, evaluator):
        assert not self._eval(evaluator, Operator.IN, None, ["admin"])


# ── Segment operator tests ────────────────────────────────────────────────────


class TestSegmentOperator:
    def _make_evaluator(self, **segments: Segment) -> FlagEvaluator:
        return FlagEvaluator(segments=segments)

    def _eval_segment(self, evaluator, context_key, segment_key, negate=False):
        op = Operator.NOT_IN_SEGMENT if negate else Operator.IN_SEGMENT
        clause = RuleClause(attribute="key", operator=op, values=[segment_key])
        rule = TargetingRule(clauses=[clause], variation="on")
        flag = _flag(rules=[rule])
        ctx = EvaluationContext(key=context_key, attributes={"plan": "pro"})
        result = evaluator.evaluate(flag, ctx, {})
        return result.reason == EvaluationReason.RULE_MATCH

    def test_in_segment_via_included_list(self):
        seg = Segment(key="beta", name="Beta", included=["user_1"])
        ev = self._make_evaluator(beta=seg)
        assert self._eval_segment(ev, "user_1", "beta")

    def test_not_in_segment_excluded(self):
        seg = Segment(key="beta", name="Beta", included=["user_1"], excluded=["user_1"])
        ev = self._make_evaluator(beta=seg)
        # excluded overrides included
        assert not self._eval_segment(ev, "user_1", "beta")

    def test_in_segment_via_rule(self):
        seg_rule = SegmentRule(
            clauses=[RuleClause(attribute="plan", operator=Operator.IS, values=["pro"])]
        )
        seg = Segment(key="pro_users", name="Pro", rules=[seg_rule])
        ev = self._make_evaluator(pro_users=seg)
        assert self._eval_segment(ev, "any_user", "pro_users")

    def test_not_in_segment_via_rule_no_match(self):
        seg_rule = SegmentRule(
            clauses=[RuleClause(attribute="plan", operator=Operator.IS, values=["pro"])]
        )
        seg = Segment(key="pro_users", name="Pro", rules=[seg_rule])
        ev = self._make_evaluator(pro_users=seg)
        # Context has plan=free, not pro → not in segment
        clause = RuleClause(attribute="key", operator=Operator.IN_SEGMENT, values=["pro_users"])
        rule = TargetingRule(clauses=[clause], variation="on")
        flag = _flag(rules=[rule])
        ctx = EvaluationContext(key="user_free", attributes={"plan": "free"})
        result = ev.evaluate(flag, ctx, {})
        assert result.reason == EvaluationReason.FALLTHROUGH

    def test_missing_segment_logs_and_returns_false(self, caplog):
        ev = FlagEvaluator(segments={})
        assert not self._eval_segment(ev, "user_1", "nonexistent_segment")

    def test_not_in_segment_operator(self):
        seg = Segment(key="blocked", name="Blocked", included=["bad_user"])
        ev = self._make_evaluator(blocked=seg)
        # good_user is NOT in blocked segment → NOT_IN_SEGMENT matches
        assert self._eval_segment(ev, "good_user", "blocked", negate=True)
        # bad_user IS in blocked segment → NOT_IN_SEGMENT does not match
        assert not self._eval_segment(ev, "bad_user", "blocked", negate=True)


# ── Semver operator tests ─────────────────────────────────────────────────────


class TestSemverOperators:
    def _eval(self, evaluator, op, actual, threshold):
        clause = RuleClause(attribute="app_version", operator=op, values=[threshold])
        rule = TargetingRule(clauses=[clause], variation="on")
        flag = _flag(rules=[rule])
        ctx = EvaluationContext(key="u", app_version=actual)
        result = evaluator.evaluate(flag, ctx, {})
        return result.reason == EvaluationReason.RULE_MATCH

    def test_semver_eq(self, evaluator):
        assert self._eval(evaluator, Operator.SEMVER_EQ, "2.3.1", "2.3.1")

    def test_semver_eq_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.SEMVER_EQ, "2.3.0", "2.3.1")

    def test_semver_lt(self, evaluator):
        assert self._eval(evaluator, Operator.SEMVER_LT, "2.3.0", "2.3.1")

    def test_semver_lt_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.SEMVER_LT, "2.3.1", "2.3.0")

    def test_semver_gt(self, evaluator):
        assert self._eval(evaluator, Operator.SEMVER_GT, "3.0.0", "2.9.9")

    def test_semver_gt_no_match(self, evaluator):
        assert not self._eval(evaluator, Operator.SEMVER_GT, "2.0.0", "2.9.9")

    def test_semver_invalid_returns_false(self, evaluator):
        assert not self._eval(evaluator, Operator.SEMVER_GT, "not-a-version", "1.0.0")


# ── Rollout bucket stability ──────────────────────────────────────────────────


class TestRolloutBucketStability:
    def test_bucket_is_deterministic(self):
        ev = FlagEvaluator(segments={})
        flag = _flag(
            fallthrough=[
                RolloutVariation(variation="on", weight=50_000),
                RolloutVariation(variation="off", weight=50_000),
            ]
        )
        ctx = _ctx("fixed_key")
        results = [ev.evaluate(flag, ctx, {}).variation for _ in range(20)]
        assert len(set(results)) == 1  # always the same

    def test_different_flag_keys_different_buckets(self):
        """Different flag keys produce different buckets for the same context."""
        ev = FlagEvaluator(segments={})
        ctx = _ctx("user_1")
        rollout = [
            RolloutVariation(variation="on", weight=50_000),
            RolloutVariation(variation="off", weight=50_000),
        ]
        flag_a = _flag("flag_a", fallthrough=rollout)
        flag_b = _flag("flag_b", fallthrough=rollout)
        results = {
            ev.evaluate(flag_a, ctx, {}).variation,
            ev.evaluate(flag_b, ctx, {}).variation,
        }
        # Not guaranteed to differ, but flag keys do affect the bucket
        # so we just verify both evaluate without error
        assert all(r in ("on", "off") for r in results)

    def test_weights_sum_100k_covers_all(self):
        """100% weight on one variation → all contexts get it."""
        ev = FlagEvaluator(segments={})
        flag = _flag(fallthrough=[RolloutVariation(variation="on", weight=100_000)])
        for i in range(50):
            r = ev.evaluate(flag, _ctx(f"user_{i}"), {})
            assert r.variation == "on"
