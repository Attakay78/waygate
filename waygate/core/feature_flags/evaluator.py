"""Pure feature flag evaluation engine.

No I/O, no async, no openfeature dependency.  Fully unit-testable in
isolation by constructing ``FeatureFlag`` and ``EvaluationContext``
objects directly.

Evaluation order
----------------
1. Flag disabled (``enabled=False``) → ``off_variation``
2. Prerequisites — recursive, short-circuits on first failure
3. Individual targets — ``flag.targets[variation]`` contains ``ctx.key``
4. Rules — top-to-bottom, first matching rule wins
5. Fallthrough — fixed variation or percentage rollout bucket

Clause semantics
----------------
- All clauses within a rule are AND-ed (all must match).
- Multiple values within one clause are OR-ed (any value must match).
- ``negate=True`` inverts the final result of the clause.

Rollout bucketing
-----------------
SHA-1 hash of ``"{flag_key}:{ctx.kind}:{ctx.key}"`` modulo 100_000.
Deterministic and stable — the same context always lands in the same
bucket.  Weights in ``RolloutVariation`` lists should sum to 100_000.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from waygate.core.feature_flags.models import (
    EvaluationContext,
    EvaluationReason,
    FeatureFlag,
    Operator,
    ResolutionDetails,
    RolloutVariation,
    RuleClause,
    Segment,
    TargetingRule,
)

logger = logging.getLogger(__name__)

# Maximum prerequisite recursion depth to prevent accidental infinite loops.
_MAX_PREREQ_DEPTH = 10


class FlagEvaluator:
    """Evaluate feature flags against an evaluation context.

    Parameters
    ----------
    segments:
        Preloaded mapping of segment key → ``Segment``.  Pass an empty
        dict if no segments are defined.  Updated in-place by the
        provider on hot-reload.

    Examples
    --------
    ::

        evaluator = FlagEvaluator(segments={"beta": beta_segment})
        result = evaluator.evaluate(flag, ctx, all_flags)
        print(result.value, result.reason)
    """

    def __init__(self, segments: dict[str, Segment]) -> None:
        self._segments = segments

    # ── Public interface ────────────────────────────────────────────────────

    def evaluate(
        self,
        flag: FeatureFlag,
        ctx: EvaluationContext,
        all_flags: dict[str, FeatureFlag],
        *,
        _depth: int = 0,
    ) -> ResolutionDetails:
        """Evaluate *flag* for *ctx* and return a ``ResolutionDetails``.

        Parameters
        ----------
        flag:
            The flag to evaluate.
        ctx:
            Per-request evaluation context.
        all_flags:
            Full flag map — required for prerequisite resolution.
        _depth:
            Internal recursion counter.  Do not pass from call sites.
        """
        if _depth > _MAX_PREREQ_DEPTH:
            logger.error(
                "waygate flags: prerequisite depth limit reached for flag '%s'. "
                "Serving off_variation to prevent infinite recursion.",
                flag.key,
            )
            return self._off(
                flag,
                reason=EvaluationReason.ERROR,
                error_message="Prerequisite depth limit exceeded",
            )

        # Step 1: global kill-switch
        if not flag.enabled:
            return self._off(flag, reason=EvaluationReason.OFF)

        # Step 2: prerequisites
        for prereq in flag.prerequisites:
            prereq_flag = all_flags.get(prereq.flag_key)
            if prereq_flag is None:
                logger.warning(
                    "waygate flags: prerequisite flag '%s' not found "
                    "for flag '%s'. Serving off_variation.",
                    prereq.flag_key,
                    flag.key,
                )
                return self._off(
                    flag,
                    reason=EvaluationReason.PREREQUISITE_FAIL,
                    prerequisite_key=prereq.flag_key,
                )
            prereq_result = self.evaluate(prereq_flag, ctx, all_flags, _depth=_depth + 1)
            if prereq_result.variation != prereq.variation:
                return self._off(
                    flag,
                    reason=EvaluationReason.PREREQUISITE_FAIL,
                    prerequisite_key=prereq.flag_key,
                )

        # Step 3: individual targets
        for variation_name, keys in flag.targets.items():
            if ctx.key in keys:
                return ResolutionDetails(
                    value=flag.get_variation_value(variation_name),
                    variation=variation_name,
                    reason=EvaluationReason.TARGET_MATCH,
                )

        # Step 4: targeting rules (top-to-bottom, first match wins)
        for rule in flag.rules:
            if self._rule_matches(rule, ctx):
                variation_name = self._resolve_rule_variation(rule, ctx, flag)
                return ResolutionDetails(
                    value=flag.get_variation_value(variation_name),
                    variation=variation_name,
                    reason=EvaluationReason.RULE_MATCH,
                    rule_id=rule.id,
                )

        # Step 5: fallthrough (default rule)
        variation_name = self._resolve_fallthrough(flag, ctx)
        return ResolutionDetails(
            value=flag.get_variation_value(variation_name),
            variation=variation_name,
            reason=EvaluationReason.FALLTHROUGH,
        )

    # ── Rule and clause matching ────────────────────────────────────────────

    def _rule_matches(self, rule: TargetingRule, ctx: EvaluationContext) -> bool:
        """Return ``True`` if ALL clauses in *rule* match *ctx* (AND logic)."""
        return all(self._clause_matches(clause, ctx) for clause in rule.clauses)

    def _clause_matches(self, clause: RuleClause, ctx: EvaluationContext) -> bool:
        """Evaluate a single clause against the context.

        Applies the operator, then inverts the result if ``negate=True``.
        Returns ``False`` when the attribute is missing and the operator
        requires a value (safe default — missing attribute → no match).
        """
        attrs = ctx.all_attributes()
        actual = attrs.get(clause.attribute)
        result = self._apply_operator(clause.operator, actual, clause.values)
        return not result if clause.negate else result

    def _apply_operator(self, op: Operator, actual: Any, values: list[Any]) -> bool:
        """Apply *op* comparing *actual* against *values*.

        Multiple values use OR logic — returns ``True`` if any value matches.
        Missing ``actual`` (``None``) returns ``False`` for all operators
        except ``IS_NOT`` and ``NOT_IN``.
        """
        # Segment operators delegate to _in_segment
        if op == Operator.IN_SEGMENT:
            return any(self._in_segment(actual, seg_key, _ctx=None) for seg_key in values)
        if op == Operator.NOT_IN_SEGMENT:
            return all(not self._in_segment(actual, seg_key, _ctx=None) for seg_key in values)

        if actual is None:
            # Only IS_NOT and NOT_IN make sense with None
            if op == Operator.IS_NOT:
                return all(v is not None for v in values)
            if op == Operator.NOT_IN:
                return None not in values
            return False

        match op:
            # ── Equality ────────────────────────────────────────────────
            case Operator.IS:
                return any(actual == v for v in values)
            case Operator.IS_NOT:
                return all(actual != v for v in values)
            # ── String ──────────────────────────────────────────────────
            case Operator.CONTAINS:
                s = str(actual)
                return any(str(v) in s for v in values)
            case Operator.NOT_CONTAINS:
                s = str(actual)
                return all(str(v) not in s for v in values)
            case Operator.STARTS_WITH:
                s = str(actual)
                return any(s.startswith(str(v)) for v in values)
            case Operator.ENDS_WITH:
                s = str(actual)
                return any(s.endswith(str(v)) for v in values)
            case Operator.MATCHES:
                s = str(actual)
                return any(_safe_regex(str(v), s) for v in values)
            case Operator.NOT_MATCHES:
                s = str(actual)
                return all(not _safe_regex(str(v), s) for v in values)
            # ── Numeric ─────────────────────────────────────────────────
            case Operator.GT:
                return _numeric_op(actual, values[0], lambda a, b: a > b)
            case Operator.GTE:
                return _numeric_op(actual, values[0], lambda a, b: a >= b)
            case Operator.LT:
                return _numeric_op(actual, values[0], lambda a, b: a < b)
            case Operator.LTE:
                return _numeric_op(actual, values[0], lambda a, b: a <= b)
            # ── Date (ISO-8601 string lexicographic comparison) ──────────
            case Operator.BEFORE:
                return str(actual) < str(values[0])
            case Operator.AFTER:
                return str(actual) > str(values[0])
            # ── Collection ──────────────────────────────────────────────
            case Operator.IN:
                return actual in values
            case Operator.NOT_IN:
                return actual not in values
            # ── Semantic version ────────────────────────────────────────
            case Operator.SEMVER_EQ:
                return _semver_op(actual, values[0], "eq")
            case Operator.SEMVER_LT:
                return _semver_op(actual, values[0], "lt")
            case Operator.SEMVER_GT:
                return _semver_op(actual, values[0], "gt")
            case _:
                logger.warning("waygate flags: unknown operator '%s'", op)
                return False

    # ── Segment evaluation ──────────────────────────────────────────────────

    def _in_segment(
        self,
        context_key: str | None,
        segment_key: str,
        *,
        _ctx: EvaluationContext | None,
    ) -> bool:
        """Return ``True`` if *context_key* is a member of *segment_key*.

        Evaluation order:
        1. Key in ``excluded`` → False
        2. Key in ``included`` → True
        3. Any segment rule matches → True
        4. Otherwise → False
        """
        if context_key is None:
            return False

        seg = self._segments.get(segment_key)
        if seg is None:
            logger.warning(
                "waygate flags: segment '%s' not found — treating as empty.",
                segment_key,
            )
            return False

        if context_key in seg.excluded:
            return False
        if context_key in seg.included:
            return True

        if _ctx is None:
            # Segment rules need the full context — called from a clause
            # that only passed the context key, not the full EvaluationContext.
            # Without the full context we can't evaluate rules.
            return False

        for rule in seg.rules:
            if all(self._clause_matches(clause, _ctx) for clause in rule.clauses):
                return True

        return False

    def _clause_matches_with_ctx(self, clause: RuleClause, ctx: EvaluationContext) -> bool:
        """Clause match variant that passes *ctx* into segment evaluation."""
        if clause.operator in (Operator.IN_SEGMENT, Operator.NOT_IN_SEGMENT):
            actual = ctx.key
            if clause.operator == Operator.IN_SEGMENT:
                result = any(
                    self._in_segment(actual, seg_key, _ctx=ctx) for seg_key in clause.values
                )
            else:
                result = all(
                    not self._in_segment(actual, seg_key, _ctx=ctx) for seg_key in clause.values
                )
            return not result if clause.negate else result
        return self._clause_matches(clause, ctx)

    def _rule_matches(self, rule: TargetingRule, ctx: EvaluationContext) -> bool:  # type: ignore[no-redef]
        """Return ``True`` if ALL clauses in *rule* match *ctx*.

        Uses ``_clause_matches_with_ctx`` so that segment operators receive
        the full context for rule evaluation.
        """
        return all(self._clause_matches_with_ctx(clause, ctx) for clause in rule.clauses)

    # ── Rollout and variation resolution ───────────────────────────────────

    def _resolve_rule_variation(
        self, rule: TargetingRule, ctx: EvaluationContext, flag: FeatureFlag
    ) -> str:
        """Return the variation name to serve for a matched rule."""
        if rule.variation is not None:
            return rule.variation
        if rule.rollout:
            return self._bucket_rollout(rule.rollout, ctx, flag.key)
        # Malformed rule — fall through to flag default
        logger.warning(
            "waygate flags: rule '%s' on flag '%s' has neither variation "
            "nor rollout — falling through to default.",
            rule.id,
            flag.key,
        )
        return self._resolve_fallthrough(flag, ctx)

    def _resolve_fallthrough(self, flag: FeatureFlag, ctx: EvaluationContext) -> str:
        """Return the variation name for the fallthrough (default) rule."""
        if isinstance(flag.fallthrough, str):
            return flag.fallthrough
        return self._bucket_rollout(flag.fallthrough, ctx, flag.key)

    @staticmethod
    def _bucket_rollout(
        rollout: list[RolloutVariation],
        ctx: EvaluationContext,
        flag_key: str,
    ) -> str:
        """Deterministic bucket assignment for percentage rollouts.

        Uses SHA-1 of ``"{flag_key}:{ctx.kind}:{ctx.key}"`` for stable,
        consistent assignment.  Bucket range is 0–99_999 (100_000 total)
        matching the weight precision of ``RolloutVariation.weight``.

        Returns the last variation if weights don't sum to 100_000 (safe
        fallback — never raises).
        """
        seed = f"{flag_key}:{ctx.kind}:{ctx.key}"
        bucket = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % 100_000
        cumulative = 0
        for rv in rollout:
            cumulative += rv.weight
            if bucket < cumulative:
                return rv.variation
        return rollout[-1].variation

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _off(
        flag: FeatureFlag,
        *,
        reason: EvaluationReason,
        prerequisite_key: str | None = None,
        error_message: str | None = None,
    ) -> ResolutionDetails:
        return ResolutionDetails(
            value=flag.get_variation_value(flag.off_variation),
            variation=flag.off_variation,
            reason=reason,
            prerequisite_key=prerequisite_key,
            error_message=error_message,
        )


# ── Module-level helpers ──────────────────────────────────────────────────────


def _safe_regex(pattern: str, string: str) -> bool:
    """Apply regex *pattern* to *string*, returning ``False`` on error."""
    try:
        return bool(re.search(pattern, string))
    except re.error as exc:
        logger.warning("waygate flags: invalid regex '%s': %s", pattern, exc)
        return False


def _numeric_op(actual: Any, threshold: Any, comparator: Any) -> bool:
    """Apply a numeric comparison, returning ``False`` on type errors."""
    try:
        return comparator(float(actual), float(threshold))  # type: ignore[no-any-return]
    except (TypeError, ValueError):
        return False


def _semver_op(actual: Any, threshold: Any, op: str) -> bool:
    """Apply a semantic version comparison using ``packaging.version``.

    Falls back to ``False`` if ``packaging`` is not installed or the
    version strings are malformed.
    """
    try:
        from packaging.version import Version

        a = Version(str(actual))
        b = Version(str(threshold))
        if op == "eq":
            return a == b
        if op == "lt":
            return a < b
        if op == "gt":
            return a > b
    except ImportError:
        logger.warning(
            "waygate flags: semver operators require 'packaging'. "
            "Install with: pip install waygate[flags]"
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "waygate flags: semver comparison failed for values '%s' and '%s'.",
            actual,
            threshold,
        )
    return False
