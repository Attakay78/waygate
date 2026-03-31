"""waygate — route lifecycle management for Python APIs.

All public symbols are available directly from this package::

    from waygate import (
        WaygateEngine,
        make_engine,
        MemoryBackend,
        FileBackend,
        RouteState,
        AuditEntry,
        MaintenanceWindow,
        RateLimitPolicy,
        WaygateException,
        SlackWebhookFormatter,
        default_formatter,
        # feature flag models (requires waygate[flags])
        FeatureFlag,
        EvaluationContext,
    )

Framework adapters live in their own namespaces::

    from waygate.fastapi import WaygateAdmin, WaygateMiddleware, WaygateRouter, ...
    from waygate.sdk import WaygateSDK
    from waygate.server import WaygateServer
"""

# ---------------------------------------------------------------------------
# Engine & config
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
from waygate.core.backends.base import WaygateBackend
from waygate.core.backends.file import FileBackend
from waygate.core.backends.memory import MemoryBackend
from waygate.core.config import make_backend, make_engine
from waygate.core.engine import WaygateEngine

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
from waygate.core.exceptions import (
    AmbiguousRouteError,
    EnvGatedException,
    MaintenanceException,
    RateLimitExceededException,
    RouteDisabledException,
    RouteNotFoundException,
    RouteProtectedException,
    WaygateException,
    WaygateProductionWarning,
)

# ---------------------------------------------------------------------------
# Feature flag models  (pure Pydantic — safe without the [flags] extra)
# ---------------------------------------------------------------------------
from waygate.core.feature_flags.evaluator import FlagEvaluator
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
    Segment,
    SegmentRule,
    TargetingRule,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
from waygate.core.models import (
    AuditEntry,
    GlobalMaintenanceConfig,
    MaintenanceWindow,
    RouteState,
    RouteStatus,
)

# ---------------------------------------------------------------------------
# Rate limiting models
# ---------------------------------------------------------------------------
from waygate.core.rate_limit.models import (
    GlobalRateLimitPolicy,
    OnMissingKey,
    RateLimitAlgorithm,
    RateLimitHit,
    RateLimitKeyStrategy,
    RateLimitPolicy,
    RateLimitResult,
    RateLimitTier,
)

# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
from waygate.core.webhooks import SlackWebhookFormatter, default_formatter

# ---------------------------------------------------------------------------
# RedisBackend — available only when the [redis] extra is installed
# ---------------------------------------------------------------------------
try:
    from waygate.core.backends.redis import RedisBackend
except ImportError:
    pass

__all__ = [
    # Engine & config
    "WaygateEngine",
    "make_engine",
    "make_backend",
    # Backends
    "WaygateBackend",
    "MemoryBackend",
    "FileBackend",
    "RedisBackend",
    # Models
    "RouteStatus",
    "RouteState",
    "AuditEntry",
    "MaintenanceWindow",
    "GlobalMaintenanceConfig",
    # Exceptions
    "WaygateException",
    "MaintenanceException",
    "EnvGatedException",
    "RouteDisabledException",
    "RouteNotFoundException",
    "AmbiguousRouteError",
    "RouteProtectedException",
    "RateLimitExceededException",
    "WaygateProductionWarning",
    # Webhooks
    "default_formatter",
    "SlackWebhookFormatter",
    # Rate limiting
    "RateLimitAlgorithm",
    "OnMissingKey",
    "RateLimitKeyStrategy",
    "RateLimitTier",
    "RateLimitPolicy",
    "RateLimitResult",
    "GlobalRateLimitPolicy",
    "RateLimitHit",
    # Feature flags
    "FlagEvaluator",
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
    "Segment",
    "SegmentRule",
    "TargetingRule",
]
