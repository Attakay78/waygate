"""WaygateOpenFeatureProvider — native OpenFeature provider backed by WaygateBackend.

Phase 2 implementation. Stub present so the package imports cleanly.
Full implementation wires FlagEvaluator into the OpenFeature resolution API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from waygate.core.feature_flags._guard import _require_flags

_require_flags()

from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.provider import AbstractProvider
from openfeature.provider.metadata import Metadata

from waygate.core.feature_flags._context import from_of_context
from waygate.core.feature_flags.evaluator import FlagEvaluator
from waygate.core.feature_flags.models import (
    EvaluationReason,
    FeatureFlag,
    Segment,
)

if TYPE_CHECKING:
    from waygate.core.backends.base import WaygateBackend

logger = logging.getLogger(__name__)

# Map Waygate EvaluationReason → OpenFeature Reason string
_REASON_MAP: dict[EvaluationReason, str] = {
    EvaluationReason.OFF: Reason.DISABLED,
    EvaluationReason.FALLTHROUGH: Reason.DEFAULT,
    EvaluationReason.TARGET_MATCH: Reason.TARGETING_MATCH,
    EvaluationReason.RULE_MATCH: Reason.TARGETING_MATCH,
    EvaluationReason.PREREQUISITE_FAIL: Reason.DISABLED,
    EvaluationReason.ERROR: Reason.ERROR,
    EvaluationReason.DEFAULT: Reason.DEFAULT,
}


class WaygateOpenFeatureProvider(AbstractProvider):
    """OpenFeature-compliant provider backed by ``WaygateBackend``.

    Stores ``FeatureFlag`` and ``Segment`` objects in the same backend
    as ``RouteState`` — no separate infrastructure required.

    Subscribes to backend pub/sub for instant hot-reload on flag changes.
    Evaluates flags locally using ``FlagEvaluator`` — zero network calls
    per evaluation.

    Parameters
    ----------
    backend:
        The ``WaygateBackend`` instance (Memory, File, or Redis).
        Must be the same instance passed to ``WaygateEngine``.
    """

    def __init__(self, backend: WaygateBackend) -> None:
        self._backend = backend
        self._flags: dict[str, FeatureFlag] = {}
        self._segments: dict[str, Segment] = {}
        self._evaluator = FlagEvaluator(segments=self._segments)

    def get_metadata(self) -> Metadata:
        return Metadata(name="waygate")

    def get_provider_hooks(self) -> list[Any]:
        return []

    def initialize(self, evaluation_context: Any = None) -> None:
        """No-op sync hook required by the OpenFeature SDK registry.

        The OpenFeature SDK calls this synchronously when ``set_provider()``
        is invoked.  Actual async initialisation (loading flags from the
        backend) is performed by ``engine.start()`` via ``_load_all()``.
        """

    def shutdown(self) -> None:
        """No-op sync hook required by the OpenFeature SDK registry."""

    async def _load_all(self) -> None:
        """Load all flags and segments from backend into local cache."""
        try:
            flags = await self._backend.load_all_flags()
            self._flags = {f.key: f for f in flags}
            segments = await self._backend.load_all_segments()
            self._segments.update({s.key: s for s in segments})
        except AttributeError:
            # Backend does not yet support flag storage (pre-Phase 3 backends).
            # Operate with empty caches — all evaluations return defaults.
            logger.debug(
                "waygate flags: backend does not support flag storage yet. "
                "All flag evaluations will return defaults."
            )

    # ── OpenFeature resolution methods ──────────────────────────────────────

    def resolve_boolean_details(
        self, flag_key: str, default_value: bool, evaluation_context: Any = None
    ) -> FlagResolutionDetails[Any]:
        return self._resolve(flag_key, default_value, evaluation_context, bool)

    def resolve_string_details(
        self, flag_key: str, default_value: str, evaluation_context: Any = None
    ) -> FlagResolutionDetails[Any]:
        return self._resolve(flag_key, default_value, evaluation_context, str)

    def resolve_integer_details(
        self, flag_key: str, default_value: int, evaluation_context: Any = None
    ) -> FlagResolutionDetails[Any]:
        return self._resolve(flag_key, default_value, evaluation_context, int)

    def resolve_float_details(
        self, flag_key: str, default_value: float, evaluation_context: Any = None
    ) -> FlagResolutionDetails[Any]:
        return self._resolve(flag_key, default_value, evaluation_context, float)

    def resolve_object_details(  # type: ignore[override]
        self,
        flag_key: str,
        default_value: dict[str, Any],
        evaluation_context: Any = None,
    ) -> FlagResolutionDetails[Any]:
        return self._resolve(flag_key, default_value, evaluation_context, dict)

    # ── Internal ────────────────────────────────────────────────────────────

    def _resolve(
        self,
        flag_key: str,
        default_value: Any,
        of_ctx: Any,
        expected_type: type,
    ) -> FlagResolutionDetails[Any]:
        flag = self._flags.get(flag_key)
        if flag is None:
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.DEFAULT,
                error_code=ErrorCode.FLAG_NOT_FOUND,
                error_message=f"Flag '{flag_key}' not found",
            )

        ctx = from_of_context(of_ctx)
        try:
            result = self._evaluator.evaluate(flag, ctx, self._flags)
        except Exception as exc:  # noqa: BLE001
            logger.exception("waygate flags: evaluation error for '%s'", flag_key)
            return FlagResolutionDetails(
                value=default_value,
                reason=Reason.ERROR,
                error_code=ErrorCode.GENERAL,
                error_message=str(exc),
            )

        value = result.value
        # Type coercion — ensure returned value matches the expected type
        if value is None:
            value = default_value
        else:
            try:
                value = expected_type(value)
            except (TypeError, ValueError):
                value = default_value

        flag_metadata: dict[str, int | float | str] = {}
        if result.rule_id is not None:
            flag_metadata["rule_id"] = result.rule_id
        if result.prerequisite_key is not None:
            flag_metadata["prerequisite_key"] = result.prerequisite_key
        return FlagResolutionDetails(
            value=value,
            variant=result.variation,
            reason=_REASON_MAP.get(result.reason, Reason.UNKNOWN),
            flag_metadata=flag_metadata,
        )

    # ── Flag cache management (called by engine on flag CRUD) ────────────────

    def upsert_flag(self, flag: FeatureFlag) -> None:
        """Update or insert a flag in the local cache."""
        self._flags[flag.key] = flag

    def delete_flag(self, flag_key: str) -> None:
        """Remove a flag from the local cache."""
        self._flags.pop(flag_key, None)

    def upsert_segment(self, segment: Segment) -> None:
        """Update or insert a segment in the local cache."""
        self._segments[segment.key] = segment

    def delete_segment(self, segment_key: str) -> None:
        """Remove a segment from the local cache."""
        self._segments.pop(segment_key, None)
