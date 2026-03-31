"""waygate.core.feature_flags — OpenFeature-compliant feature flag system.

This package requires the [flags] optional extra::

    pip install waygate[flags]

Importing from this package when the extra is not installed raises an
``ImportError`` with clear installation instructions.

All public symbols are re-exported under Waygate-namespaced names.
``openfeature`` never appears in user-facing imports.

Usage
-----
::

    from waygate.core.feature_flags import (
        EvaluationContext,
        WaygateFeatureClient,
        EvaluationReason,
        ResolutionDetails,
    )

    ctx = EvaluationContext(key=user_id, attributes={"plan": "pro"})
    value = await flag_client.get_boolean_value("new_checkout", False, ctx)

Custom provider (implements OpenFeature's AbstractProvider)::

    from waygate.core.feature_flags import WaygateFlagProvider

    class MyProvider(WaygateFlagProvider):
        ...

Custom hook (implements OpenFeature's Hook interface)::

    from waygate.core.feature_flags import WaygateHook
"""

from __future__ import annotations

# ── Guard: raise early with a helpful message if openfeature not installed ──
from waygate.core.feature_flags._guard import _require_flags

_require_flags()

# ── OpenFeature ABC re-exports (Waygate-namespaced) ──────────────────────────
# These are the extension points for users who want custom providers/hooks.
from openfeature.hook import Hook as WaygateHook
from openfeature.provider import AbstractProvider as WaygateFlagProvider

# ── Client and provider re-exports ──────────────────────────────────────────
# Imported lazily here so the module graph stays clean.
# client.py and provider.py each call _require_flags() themselves.
from waygate.core.feature_flags.client import WaygateFeatureClient as WaygateFeatureClient

# ── Hook re-exports ─────────────────────────────────────────────────────────
from waygate.core.feature_flags.hooks import (
    AuditHook as AuditHook,
)
from waygate.core.feature_flags.hooks import (
    LoggingHook as LoggingHook,
)
from waygate.core.feature_flags.hooks import (
    MetricsHook as MetricsHook,
)
from waygate.core.feature_flags.hooks import (
    OpenTelemetryHook as OpenTelemetryHook,
)

# ── Waygate-native model re-exports ──────────────────────────────────────────
from waygate.core.feature_flags.models import (
    EvaluationContext as EvaluationContext,
)
from waygate.core.feature_flags.models import (
    EvaluationReason as EvaluationReason,
)
from waygate.core.feature_flags.models import (
    FeatureFlag as FeatureFlag,
)
from waygate.core.feature_flags.models import (
    FlagStatus as FlagStatus,
)
from waygate.core.feature_flags.models import (
    FlagType as FlagType,
)
from waygate.core.feature_flags.models import (
    FlagVariation as FlagVariation,
)
from waygate.core.feature_flags.models import (
    Operator as Operator,
)
from waygate.core.feature_flags.models import (
    Prerequisite as Prerequisite,
)
from waygate.core.feature_flags.models import (
    ResolutionDetails as ResolutionDetails,
)
from waygate.core.feature_flags.models import (
    RolloutVariation as RolloutVariation,
)
from waygate.core.feature_flags.models import (
    RuleClause as RuleClause,
)
from waygate.core.feature_flags.models import (
    ScheduledChange as ScheduledChange,
)
from waygate.core.feature_flags.models import (
    ScheduledChangeAction as ScheduledChangeAction,
)
from waygate.core.feature_flags.models import (
    Segment as Segment,
)
from waygate.core.feature_flags.models import (
    SegmentRule as SegmentRule,
)
from waygate.core.feature_flags.models import (
    TargetingRule as TargetingRule,
)
from waygate.core.feature_flags.provider import (
    WaygateOpenFeatureProvider as WaygateOpenFeatureProvider,
)

__all__ = [
    # Extension points
    "WaygateFlagProvider",
    "WaygateHook",
    # Models
    "EvaluationContext",
    "EvaluationReason",
    "FeatureFlag",
    "FlagStatus",
    "FlagType",
    "FlagVariation",
    "Operator",
    "Prerequisite",
    "ResolutionDetails",
    "RolloutVariation",
    "RuleClause",
    "ScheduledChange",
    "ScheduledChangeAction",
    "Segment",
    "SegmentRule",
    "TargetingRule",
    # Client
    "WaygateFeatureClient",
    # Provider
    "WaygateOpenFeatureProvider",
    # Hooks
    "AuditHook",
    "LoggingHook",
    "MetricsHook",
    "OpenTelemetryHook",
]
