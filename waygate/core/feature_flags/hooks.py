"""Built-in OpenFeature hooks for waygate.

All hooks implement OpenFeature's ``Hook`` interface and are registered
via ``engine.use_openfeature(hooks=[...])``.

Built-in hooks registered by default
-------------------------------------
``LoggingHook`` — logs every evaluation at DEBUG level.
``AuditHook``   — records non-trivial evaluations in WaygateEngine's audit log.
``MetricsHook`` — increments per-variation counters for dashboard stats.

Optional hooks (user-registered)
---------------------------------
``OpenTelemetryHook`` — sets ``feature_flag.*`` span attributes on the
current OpenTelemetry span.  Requires ``opentelemetry-api`` to be installed.
"""

from __future__ import annotations

import logging
from typing import Any

from waygate.core.feature_flags._guard import _require_flags

_require_flags()

from openfeature.flag_evaluation import FlagEvaluationDetails, FlagValueType
from openfeature.hook import Hook, HookContext, HookHints

logger = logging.getLogger(__name__)


class LoggingHook(Hook):
    """Log every flag evaluation at DEBUG level.

    Automatically registered by ``engine.use_openfeature()``.
    """

    def after(
        self,
        hook_context: HookContext,
        details: FlagEvaluationDetails[FlagValueType],
        hints: HookHints,
    ) -> None:
        logger.debug(
            "waygate flag eval: key=%s variant=%s reason=%s",
            hook_context.flag_key,
            details.variant,
            details.reason,
        )

    def error(
        self,
        hook_context: HookContext,
        exception: Exception,
        hints: HookHints,
    ) -> None:
        logger.error(
            "waygate flag error: key=%s error=%s",
            hook_context.flag_key,
            exception,
        )


class AuditHook(Hook):
    """Record flag evaluations in WaygateEngine's audit log.

    Only records evaluations with non-trivial reasons (RULE_MATCH,
    TARGET_MATCH, PREREQUISITE_FAIL, ERROR) to avoid polluting the audit
    log with FALLTHROUGH and DEFAULT entries.

    Automatically registered by ``engine.use_openfeature()``.

    Parameters
    ----------
    engine:
        The ``WaygateEngine`` instance to write audit entries to.
    """

    # Reasons worth recording
    _RECORD_REASONS = frozenset(["TARGETING_MATCH", "DISABLED", "ERROR"])

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def after(
        self,
        hook_context: HookContext,
        details: FlagEvaluationDetails[FlagValueType],
        hints: HookHints,
    ) -> None:
        if details.reason not in self._RECORD_REASONS:
            return
        # Fire-and-forget — audit writes are best-effort
        import asyncio
        import contextlib

        loop = asyncio.get_event_loop()
        if loop.is_running():
            with contextlib.suppress(Exception):
                loop.create_task(
                    self._engine.record_flag_evaluation(hook_context.flag_key, details)
                )


class MetricsHook(Hook):
    """Increment per-variation evaluation counters.

    Parameters
    ----------
    collector:
        ``FlagMetricsCollector`` instance that stores the counters.
    """

    def __init__(self, collector: Any = None) -> None:
        self._collector = collector

    def after(
        self,
        hook_context: HookContext,
        details: FlagEvaluationDetails[FlagValueType],
        hints: HookHints,
    ) -> None:
        import asyncio
        import contextlib

        ctx = hook_context.evaluation_context
        targeting_key = getattr(ctx, "targeting_key", "anonymous") if ctx else "anonymous"

        record = {
            "variation": details.variant or "unknown",
            "reason": details.reason or "UNKNOWN",
            "context_key": targeting_key,
        }

        if self._collector is not None:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with contextlib.suppress(Exception):
                    loop.create_task(self._collector.record(hook_context.flag_key, record))


class OpenTelemetryHook(Hook):
    """Set ``feature_flag.*`` span attributes on the current OTel span.

    No-ops gracefully when ``opentelemetry-api`` is not installed.
    Optional — register via ``engine.use_openfeature(hooks=[OpenTelemetryHook()])``.
    """

    def after(
        self,
        hook_context: HookContext,
        details: FlagEvaluationDetails[FlagValueType],
        hints: HookHints,
    ) -> None:
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]

            span = trace.get_current_span()
            if span.is_recording():
                key = hook_context.flag_key
                span.set_attribute(f"feature_flag.{key}.value", str(details.value))
                if details.variant:
                    span.set_attribute(f"feature_flag.{key}.variant", details.variant)
                if details.reason:
                    span.set_attribute(f"feature_flag.{key}.reason", details.reason)
        except ImportError:
            pass  # opentelemetry-api not installed — silently skip
