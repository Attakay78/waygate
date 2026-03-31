"""Context conversion helpers between Waygate and OpenFeature types.

Converts ``waygate.core.feature_flags.models.EvaluationContext`` →
``openfeature.evaluation_context.EvaluationContext`` for provider dispatch,
and back again for the native provider's evaluator calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from waygate.core.feature_flags.models import EvaluationContext as WaygateContext


def to_of_context(ctx: WaygateContext | None) -> object | None:
    """Convert a Waygate ``EvaluationContext`` to an OpenFeature one.

    Returns ``None`` when *ctx* is ``None`` (OpenFeature accepts ``None``
    to mean "use global context").

    Also accepts plain ``dict`` for convenience in sync callers — the
    ``targeting_key`` entry is mapped to the OpenFeature targeting key.
    """
    if ctx is None:
        return None

    from openfeature.evaluation_context import EvaluationContext as OFContext

    if isinstance(ctx, dict):
        d = dict(ctx)
        targeting_key = d.pop("targeting_key", "anonymous")
        return OFContext(targeting_key=targeting_key, attributes=d)

    attrs = ctx.all_attributes()
    # targeting_key is the OpenFeature equivalent of our ctx.key
    targeting_key = attrs.pop("key", ctx.key)
    return OFContext(targeting_key=targeting_key, attributes=attrs)


def from_of_context(of_ctx: object | None) -> WaygateContext:
    """Convert an OpenFeature ``EvaluationContext`` to a Waygate one.

    Used inside ``WaygateOpenFeatureProvider`` when the OpenFeature SDK
    dispatches a resolution call so that ``FlagEvaluator`` receives the
    right type.
    """
    from waygate.core.feature_flags.models import EvaluationContext as WaygateContext

    if of_ctx is None:
        return WaygateContext(key="anonymous")

    # OpenFeature EvaluationContext has targeting_key + attributes
    targeting_key = getattr(of_ctx, "targeting_key", None) or "anonymous"
    attributes: dict[str, Any] = getattr(of_ctx, "attributes", {}) or {}

    return WaygateContext(
        key=targeting_key,
        kind=attributes.pop("kind", "user"),
        email=attributes.pop("email", None),
        ip=attributes.pop("ip", None),
        country=attributes.pop("country", None),
        app_version=attributes.pop("app_version", None),
        attributes=attributes,
    )
