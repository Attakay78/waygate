"""Feature flag data models for waygate.

All models are pure Pydantic v2 with no dependency on ``openfeature``.
This module is importable even without the [flags] extra installed.

Design notes
------------
``EvaluationContext.all_attributes()`` merges named convenience fields
(email, ip, country, app_version) with the free-form ``attributes`` dict
so that rule clauses can reference any of them by name without callers
having to manually populate ``attributes`` for common fields.

``RolloutVariation.weight`` is out of 100_000 (not 100) to allow
fine-grained rollouts like 0.1%, 33.33%, etc. — same precision as
LaunchDarkly.  Weights in a rollout list should sum to 100_000.

``FeatureFlag.targets`` maps variation name → list of context keys for
individual targeting.  Evaluated before rules (highest priority after
prerequisites).

``FeatureFlag.fallthrough`` accepts either a plain variation name
(``str``) for a fixed default, or a list of ``RolloutVariation`` for a
percentage-based default rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ── Flag type ────────────────────────────────────────────────────────────────


class FlagType(StrEnum):
    """Value type of a feature flag's variations."""

    BOOLEAN = "boolean"
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    JSON = "json"


# ── Variations ───────────────────────────────────────────────────────────────


class FlagVariation(BaseModel):
    """A single named variation of a feature flag.

    Parameters
    ----------
    name:
        Identifier used in rules, targets, fallthrough, and prerequisites.
        E.g. ``"on"``, ``"off"``, ``"control"``, ``"variant_a"``.
    value:
        The actual value returned when this variation is served.
        Must match the flag's ``type``.
    description:
        Optional human-readable note shown in the dashboard.
    """

    name: str
    value: bool | str | int | float | dict[str, Any] | list[Any]
    description: str = ""


class RolloutVariation(BaseModel):
    """One bucket in a percentage rollout.

    Parameters
    ----------
    variation:
        References ``FlagVariation.name``.
    weight:
        Share of traffic (out of 100_000).  All weights in a rollout
        list should sum to 100_000.  E.g. 25% = 25_000.
    """

    variation: str
    weight: int = Field(ge=0, le=100_000)


# ── Targeting operators ──────────────────────────────────────────────────────


class Operator(StrEnum):
    """All supported targeting rule operators.

    String operators
    ----------------
    ``IS`` / ``IS_NOT`` — exact string equality.
    ``CONTAINS`` / ``NOT_CONTAINS`` — substring match.
    ``STARTS_WITH`` / ``ENDS_WITH`` — prefix / suffix match.
    ``MATCHES`` / ``NOT_MATCHES`` — regex match (Python ``re`` module).

    Numeric operators
    -----------------
    ``GT`` / ``GTE`` / ``LT`` / ``LTE`` — numeric comparisons.

    Date operators
    --------------
    ``BEFORE`` / ``AFTER`` — ISO-8601 string comparisons (lexicographic).

    Collection operators
    --------------------
    ``IN`` / ``NOT_IN`` — membership in a list of values.

    Segment operators
    -----------------
    ``IN_SEGMENT`` / ``NOT_IN_SEGMENT`` — context is/isn't in a named segment.

    Semantic version operators
    --------------------------
    ``SEMVER_EQ`` / ``SEMVER_LT`` / ``SEMVER_GT`` — PEP 440 / semver
    comparison using ``packaging.version.Version``.
    Requires ``packaging`` (installed with the [flags] extra).
    """

    # Equality
    IS = "is"
    IS_NOT = "is_not"
    # String
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    MATCHES = "matches"
    NOT_MATCHES = "not_matches"
    # Numeric
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    # Date
    BEFORE = "before"
    AFTER = "after"
    # Collection
    IN = "in"
    NOT_IN = "not_in"
    # Segment
    IN_SEGMENT = "in_segment"
    NOT_IN_SEGMENT = "not_in_segment"
    # Semantic version
    SEMVER_EQ = "semver_eq"
    SEMVER_LT = "semver_lt"
    SEMVER_GT = "semver_gt"


# ── Rules ────────────────────────────────────────────────────────────────────


class RuleClause(BaseModel):
    """A single condition in a targeting rule.

    All clauses within a rule are AND-ed together.
    Multiple values within one clause are OR-ed (any value must match).

    Parameters
    ----------
    attribute:
        Context attribute to inspect.  E.g. ``"role"``, ``"plan"``,
        ``"email"``, ``"country"``, ``"app_version"``.
    operator:
        Comparison operator to apply.
    values:
        One or more values to compare against.  Multiple values use
        OR logic — the clause passes if *any* value matches.
    negate:
        When ``True``, the result of the operator check is inverted.
    """

    attribute: str
    operator: Operator
    values: list[Any]
    negate: bool = False


class TargetingRule(BaseModel):
    """A complete targeting rule: all clauses match → serve a variation.

    Parameters
    ----------
    id:
        UUID4 identifier.  Used for ordering, references, and scheduling.
    description:
        Human-readable label shown in the dashboard.
    clauses:
        List of ``RuleClause``.  ALL must match (AND logic).
    variation:
        Fixed variation name to serve when rule matches.
        Mutually exclusive with ``rollout``.
    rollout:
        Percentage rollout when rule matches.
        Mutually exclusive with ``variation``.
    track_events:
        When ``True``, evaluation events for this rule are always
        recorded regardless of global event sampling settings.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    clauses: list[RuleClause] = Field(default_factory=list)
    variation: str | None = None
    rollout: list[RolloutVariation] | None = None
    track_events: bool = False


# ── Prerequisites ─────────────────────────────────────────────────────────────


class Prerequisite(BaseModel):
    """A prerequisite flag that must evaluate to a specific variation.

    Parameters
    ----------
    flag_key:
        Key of the prerequisite flag.
    variation:
        The variation the prerequisite flag must return.
        If it returns any other variation, the dependent flag serves
        its ``off_variation``.
    """

    flag_key: str
    variation: str


# ── Segments ─────────────────────────────────────────────────────────────────


class SegmentRule(BaseModel):
    """A rule within a segment definition.

    If all clauses match, the context is considered part of the segment.
    Multiple segment rules are OR-ed (any matching rule → included).

    Parameters
    ----------
    id:
        UUID4 identifier for ordering and deletion.
    description:
        Optional human-readable label shown in the dashboard.
    clauses:
        List of ``RuleClause``.  ALL must match (AND logic) for the rule
        to match.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    clauses: list[RuleClause] = Field(default_factory=list)


class Segment(BaseModel):
    """A reusable group of contexts for flag targeting.

    Evaluation order:
    1. If ``context.key`` is in ``excluded`` → NOT in segment.
    2. If ``context.key`` is in ``included`` → IN segment.
    3. Evaluate ``rules`` top-to-bottom — first match → IN segment.
    4. No match → NOT in segment.

    Parameters
    ----------
    key:
        Unique identifier.  Referenced by ``IN_SEGMENT`` clauses.
    name:
        Human-readable display name.
    included:
        Explicit context keys always included in this segment.
    excluded:
        Explicit context keys always excluded (overrides rules and included).
    rules:
        Targeting rules — any matching rule means the context is included.
    tags:
        Organisational labels for filtering in the dashboard.
    """

    key: str
    name: str
    description: str = ""
    included: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    rules: list[SegmentRule] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Scheduled changes ─────────────────────────────────────────────────────────


class ScheduledChangeAction(StrEnum):
    """Action to execute at a scheduled time."""

    ENABLE = "enable"
    DISABLE = "disable"
    UPDATE_ROLLOUT = "update_rollout"
    ADD_RULE = "add_rule"
    DELETE_RULE = "delete_rule"


class ScheduledChange(BaseModel):
    """A pending change to a flag scheduled for future execution.

    Parameters
    ----------
    id:
        UUID4 identifier.
    execute_at:
        UTC datetime when the change should fire.
    action:
        Which operation to apply to the flag.
    payload:
        Action-specific data.  E.g. for ``UPDATE_ROLLOUT``::

            {"variation": "on", "weight": 50_000}

        For ``ADD_RULE``: a serialised ``TargetingRule`` dict.
        For ``DELETE_RULE``: ``{"rule_id": "..."}``.
    created_by:
        Actor who scheduled the change (username or ``"system"``).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execute_at: datetime
    action: ScheduledChangeAction
    payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Flag lifecycle status ─────────────────────────────────────────────────────


class FlagStatus(StrEnum):
    """Computed lifecycle status of a feature flag.

    Derived from evaluation metrics and configuration — not stored.
    """

    NEW = "new"
    """Created recently, never evaluated."""

    ACTIVE = "active"
    """Being evaluated or recently modified."""

    LAUNCHED = "launched"
    """Fully rolled out — single variation, stable, safe to clean up."""

    INACTIVE = "inactive"
    """Not evaluated in 7+ days."""

    DEPRECATED = "deprecated"
    """Marked deprecated by an operator.  Still evaluated if enabled."""

    ARCHIVED = "archived"
    """Removed from active use.  No longer evaluated."""


# ── Full flag definition ──────────────────────────────────────────────────────


class FeatureFlag(BaseModel):
    """Full definition of a feature flag.

    Stored in ``WaygateBackend`` alongside ``RouteState``.
    Backend storage key convention: ``waygate:flag:{key}``.

    Parameters
    ----------
    key:
        Unique identifier.  Used in code: ``flags.get_boolean_value("my-flag", ...)``.
    name:
        Human-readable display name shown in the dashboard.
    type:
        Determines valid variation value types.
    variations:
        All possible flag values.  Must contain at least two variations.
    off_variation:
        Variation served when ``enabled=False``.  Must match a name in
        ``variations``.
    fallthrough:
        Default rule when no targeting rule matches.  Either a fixed
        variation name (``str``) or a percentage rollout
        (``list[RolloutVariation]`` summing to 100_000).
    enabled:
        Global kill-switch.  When ``False``, all requests receive
        ``off_variation`` regardless of targeting rules.
    prerequisites:
        Other flags that must evaluate to specific variations before this
        flag's rules run.  Evaluated recursively.  Circular dependencies
        are prevented at write time.
    targets:
        Individual targeting.  Maps variation name → list of context keys
        that always receive that variation.  Evaluated after prerequisites,
        before rules.
    rules:
        Targeting rules evaluated top-to-bottom.  First match wins.
    scheduled_changes:
        Pending future mutations managed by ``FlagScheduler``.
    temporary:
        When ``True``, the flag hygiene system may mark it for removal
        once it reaches ``LAUNCHED`` or ``INACTIVE`` status.
    maintainer:
        Username of the person responsible for this flag.
    """

    key: str
    name: str
    description: str = ""
    type: FlagType
    tags: list[str] = Field(default_factory=list)

    variations: list[FlagVariation]
    off_variation: str
    fallthrough: str | list[RolloutVariation]

    enabled: bool = True
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    targets: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[TargetingRule] = Field(default_factory=list)
    scheduled_changes: list[ScheduledChange] = Field(default_factory=list)

    status: FlagStatus = FlagStatus.ACTIVE
    temporary: bool = True
    maintainer: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = "system"

    def get_variation_value(self, name: str) -> Any:
        """Return the value for the variation with the given name.

        Returns ``None`` if the variation name is not found — callers
        should validate variation names at write time.
        """
        for v in self.variations:
            if v.name == name:
                return v.value
        return None

    def variation_names(self) -> list[str]:
        """Return all variation names for this flag."""
        return [v.name for v in self.variations]


# ── Evaluation context ────────────────────────────────────────────────────────


class EvaluationContext(BaseModel):
    """Per-request context used for flag targeting.

    This is the primary object application code constructs and passes to
    ``WaygateFeatureClient.get_*_value()``.

    Parameters
    ----------
    key:
        Required unique identifier for the entity being evaluated.
        Typically ``user_id``, ``session_id``, or ``org_id``.  Used for
        individual targeting and deterministic rollout bucketing.
    kind:
        Context kind.  Defaults to ``"user"``.  Use ``"organization"``,
        ``"device"``, or a custom string for non-user contexts.
    email:
        Convenience field — accessible in rules as ``"email"`` attribute.
    ip:
        Convenience field — accessible in rules as ``"ip"`` attribute.
    country:
        Convenience field — accessible in rules as ``"country"`` attribute.
    app_version:
        Convenience field — accessible in rules as ``"app_version"``.
        Use semver operators for version-based targeting.
    attributes:
        Arbitrary additional attributes.  Keys must be strings.
        Values can be any JSON-serialisable type.

    Examples
    --------
    Minimal context::

        ctx = EvaluationContext(key=request.headers["x-user-id"])

    Rich context::

        ctx = EvaluationContext(
            key=user.id,
            kind="user",
            email=user.email,
            country=user.country,
            app_version="2.3.1",
            attributes={"plan": user.plan, "role": user.role},
        )
    """

    key: str
    kind: str = "user"
    email: str | None = None
    ip: str | None = None
    country: str | None = None
    app_version: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    def all_attributes(self) -> dict[str, Any]:
        """Merge named convenience fields with ``attributes`` for rule evaluation.

        Named fields take lower priority than ``attributes`` — if the same
        key appears in both, ``attributes`` wins.

        Returns
        -------
        dict[str, Any]
            Flat dict of all context attributes, including ``"key"`` and
            ``"kind"`` as first-class attributes.
        """
        base: dict[str, Any] = {"key": self.key, "kind": self.kind}
        for field_name in ("email", "ip", "country", "app_version"):
            val = getattr(self, field_name)
            if val is not None:
                base[field_name] = val
        return {**base, **self.attributes}


# ── Resolution result ─────────────────────────────────────────────────────────


class EvaluationReason(StrEnum):
    """Why a flag returned the value it did.

    Included in ``ResolutionDetails`` for every evaluation.
    Used by the live events stream, audit hook, and eval debugger.
    """

    OFF = "OFF"
    """Flag is globally disabled.  ``off_variation`` was served."""

    FALLTHROUGH = "FALLTHROUGH"
    """No targeting rule matched.  Default rule was served."""

    TARGET_MATCH = "TARGET_MATCH"
    """Context key was in the individual targets list."""

    RULE_MATCH = "RULE_MATCH"
    """A targeting rule matched.  See ``rule_id``."""

    PREREQUISITE_FAIL = "PREREQUISITE_FAIL"
    """A prerequisite flag did not return the required variation.
    See ``prerequisite_key``."""

    ERROR = "ERROR"
    """Provider or evaluation error.  Default value was returned."""

    DEFAULT = "DEFAULT"
    """Flag not found in provider.  SDK default was returned."""


class ResolutionDetails(BaseModel):
    """Full result of a feature flag evaluation.

    Application code usually only needs ``.value``.  The extra fields
    are used by hooks, the dashboard live stream, and the eval debugger.

    Parameters
    ----------
    value:
        The resolved flag value.
    variation:
        The variation name that was served.  ``None`` on error/default.
    reason:
        Why this value was returned.
    rule_id:
        The ``TargetingRule.id`` that matched.  Only set when
        ``reason == RULE_MATCH``.
    prerequisite_key:
        The flag key of the failing prerequisite.  Only set when
        ``reason == PREREQUISITE_FAIL``.
    error_message:
        Human-readable error detail.  Only set when ``reason == ERROR``.
    """

    value: Any
    variation: str | None = None
    reason: EvaluationReason
    rule_id: str | None = None
    prerequisite_key: str | None = None
    error_message: str | None = None
