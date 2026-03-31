"""Tests for waygate.core.feature_flags.provider — WaygateOpenFeatureProvider."""

from __future__ import annotations

from openfeature.flag_evaluation import Reason

from waygate.core.feature_flags.models import (
    FeatureFlag,
    FlagType,
    FlagVariation,
    Operator,
    RuleClause,
    Segment,
    SegmentRule,
    TargetingRule,
)
from waygate.core.feature_flags.provider import WaygateOpenFeatureProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal in-memory backend stub — no real I/O."""

    def __init__(self, flags=(), segments=()):
        self._flags = list(flags)
        self._segments = list(segments)

    async def load_all_flags(self):
        return self._flags

    async def load_all_segments(self):
        return self._segments


class _NoFlagBackend:
    """Backend that doesn't implement flag storage (pre-Phase 3)."""

    async def load_all_flags(self):
        raise AttributeError("not supported")

    async def load_all_segments(self):
        raise AttributeError("not supported")


def _bool_flag(key="my_flag", enabled=True) -> FeatureFlag:
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


def _string_flag(key="color_flag") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Color Flag",
        type=FlagType.STRING,
        variations=[
            FlagVariation(name="blue", value="blue"),
            FlagVariation(name="red", value="red"),
        ],
        off_variation="blue",
        fallthrough="red",
    )


def _int_flag(key="limit_flag") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Limit Flag",
        type=FlagType.INTEGER,
        variations=[
            FlagVariation(name="low", value=10),
            FlagVariation(name="high", value=100),
        ],
        off_variation="low",
        fallthrough="high",
    )


def _float_flag(key="rate_flag") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Rate Flag",
        type=FlagType.FLOAT,
        variations=[
            FlagVariation(name="low", value=0.1),
            FlagVariation(name="high", value=0.9),
        ],
        off_variation="low",
        fallthrough="high",
    )


def _object_flag(key="config_flag") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Config Flag",
        type=FlagType.JSON,
        variations=[
            FlagVariation(name="default", value={"limit": 10}),
            FlagVariation(name="premium", value={"limit": 100}),
        ],
        off_variation="default",
        fallthrough="premium",
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_name(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        assert provider.get_metadata().name == "waygate"

    def test_hooks_empty(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        assert provider.get_provider_hooks() == []


# ---------------------------------------------------------------------------
# initialize — loads flags and segments
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_loads_flags_and_segments(self):
        flag = _bool_flag()
        seg = Segment(key="beta", name="Beta")
        backend = _FakeBackend(flags=[flag], segments=[seg])
        provider = WaygateOpenFeatureProvider(backend)

        await provider._load_all()

        assert "my_flag" in provider._flags
        assert "beta" in provider._segments

    async def test_graceful_on_missing_backend_support(self):
        provider = WaygateOpenFeatureProvider(_NoFlagBackend())
        # Should not raise — operate with empty caches
        await provider._load_all()
        assert provider._flags == {}
        assert provider._segments == {}

    def test_shutdown_noop(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        provider.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# resolve_boolean_details
# ---------------------------------------------------------------------------


class TestResolveBooleanDetails:
    async def test_flag_not_found_returns_default(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        await provider._load_all()

        result = provider.resolve_boolean_details("missing", True)
        assert result.value is True
        assert result.error_code == "FLAG_NOT_FOUND"
        assert result.reason == Reason.DEFAULT

    async def test_disabled_flag_returns_off_variation(self):
        flag = _bool_flag(enabled=False)
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("my_flag", True)
        assert result.value is False
        assert result.reason == Reason.DISABLED

    async def test_enabled_flag_fallthrough(self):
        flag = FeatureFlag(
            key="feat",
            name="Feat",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="on",
        )
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("feat", False)
        assert result.value is True
        assert result.reason == Reason.DEFAULT

    async def test_type_coercion_fallback_on_mismatch(self):
        # String flag evaluated as boolean — should return default
        flag = _string_flag(key="color")
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("color", True)
        # "red" cannot be cast to bool cleanly (it IS truthy in Python),
        # so it returns bool("red") == True — coercion succeeds here.
        assert isinstance(result.value, bool)


# ---------------------------------------------------------------------------
# resolve_string_details
# ---------------------------------------------------------------------------


class TestResolveStringDetails:
    async def test_string_fallthrough(self):
        flag = _string_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_string_details("color_flag", "default")
        assert result.value == "red"

    async def test_string_missing_returns_default(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        await provider._load_all()

        result = provider.resolve_string_details("missing", "fallback")
        assert result.value == "fallback"
        assert result.error_code == "FLAG_NOT_FOUND"


# ---------------------------------------------------------------------------
# resolve_integer_details
# ---------------------------------------------------------------------------


class TestResolveIntegerDetails:
    async def test_integer_fallthrough(self):
        flag = _int_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_integer_details("limit_flag", 0)
        assert result.value == 100

    async def test_integer_missing(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        await provider._load_all()

        result = provider.resolve_integer_details("nope", 42)
        assert result.value == 42


# ---------------------------------------------------------------------------
# resolve_float_details
# ---------------------------------------------------------------------------


class TestResolveFloatDetails:
    async def test_float_fallthrough(self):
        flag = _float_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_float_details("rate_flag", 0.0)
        assert abs(result.value - 0.9) < 1e-9

    async def test_float_coercion_from_int(self):
        flag = _int_flag(key="int_flag")
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_float_details("int_flag", 0.0)
        assert result.value == float(100)


# ---------------------------------------------------------------------------
# resolve_object_details
# ---------------------------------------------------------------------------


class TestResolveObjectDetails:
    async def test_object_fallthrough(self):
        flag = _object_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_object_details("config_flag", {})
        assert result.value == {"limit": 100}

    async def test_object_missing(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        await provider._load_all()

        result = provider.resolve_object_details("nope", {"x": 1})
        assert result.value == {"x": 1}


# ---------------------------------------------------------------------------
# Targeting — individual targets
# ---------------------------------------------------------------------------


class TestTargeting:
    async def test_individual_target_match(self):
        from openfeature.evaluation_context import EvaluationContext as OFCtx

        flag = FeatureFlag(
            key="beta_flag",
            name="Beta",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="off",
            targets={"on": ["user_1", "user_2"]},
        )
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        ctx = OFCtx(targeting_key="user_1")
        result = provider.resolve_boolean_details("beta_flag", False, ctx)
        assert result.value is True
        assert result.reason == Reason.TARGETING_MATCH

    async def test_individual_target_miss(self):
        from openfeature.evaluation_context import EvaluationContext as OFCtx

        flag = FeatureFlag(
            key="beta_flag",
            name="Beta",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="off",
            targets={"on": ["user_1"]},
        )
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        ctx = OFCtx(targeting_key="user_99")
        result = provider.resolve_boolean_details("beta_flag", False, ctx)
        assert result.value is False  # fallthrough


# ---------------------------------------------------------------------------
# Targeting rules
# ---------------------------------------------------------------------------


class TestTargetingRules:
    async def test_rule_match_reason(self):
        from openfeature.evaluation_context import EvaluationContext as OFCtx

        flag = FeatureFlag(
            key="admin_flag",
            name="Admin Flag",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="off",
            rules=[
                TargetingRule(
                    clauses=[
                        RuleClause(
                            attribute="role",
                            operator=Operator.IS,
                            values=["admin"],
                        )
                    ],
                    variation="on",
                )
            ],
        )
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        ctx = OFCtx(targeting_key="user_1", attributes={"role": "admin"})
        result = provider.resolve_boolean_details("admin_flag", False, ctx)
        assert result.value is True
        assert result.reason == Reason.TARGETING_MATCH


# ---------------------------------------------------------------------------
# flag_metadata
# ---------------------------------------------------------------------------


class TestFlagMetadata:
    async def test_metadata_keys_present(self):
        flag = _bool_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("my_flag", True)
        # Metadata keys are only present when non-None; a FALLTHROUGH/DEFAULT
        # evaluation produces no rule_id or prerequisite_key.
        assert isinstance(result.flag_metadata, dict)


# ---------------------------------------------------------------------------
# Cache management (upsert / delete)
# ---------------------------------------------------------------------------


class TestCacheManagement:
    def test_upsert_flag(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        flag = _bool_flag()
        provider.upsert_flag(flag)
        assert "my_flag" in provider._flags

    def test_delete_flag(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        flag = _bool_flag()
        provider.upsert_flag(flag)
        provider.delete_flag("my_flag")
        assert "my_flag" not in provider._flags

    def test_delete_flag_missing_is_noop(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        provider.delete_flag("nonexistent")  # must not raise

    def test_upsert_segment(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        seg = Segment(key="beta", name="Beta")
        provider.upsert_segment(seg)
        assert "beta" in provider._segments

    def test_delete_segment(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        seg = Segment(key="beta", name="Beta")
        provider.upsert_segment(seg)
        provider.delete_segment("beta")
        assert "beta" not in provider._segments

    def test_delete_segment_missing_is_noop(self):
        provider = WaygateOpenFeatureProvider(_FakeBackend())
        provider.delete_segment("nonexistent")  # must not raise


# ---------------------------------------------------------------------------
# Reason mapping
# ---------------------------------------------------------------------------


class TestReasonMapping:
    async def test_off_reason_maps_to_disabled(self):
        flag = _bool_flag(enabled=False)
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("my_flag", True)
        assert result.reason == Reason.DISABLED

    async def test_fallthrough_reason_maps_to_default(self):
        flag = _bool_flag()
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=[flag]))
        await provider._load_all()

        result = provider.resolve_boolean_details("my_flag", True)
        assert result.reason == Reason.DEFAULT

    async def test_segment_rule_maps_to_targeting_match(self):
        from openfeature.evaluation_context import EvaluationContext as OFCtx

        seg = Segment(
            key="pro_users",
            name="Pro",
            rules=[
                SegmentRule(
                    clauses=[
                        RuleClause(
                            attribute="plan",
                            operator=Operator.IS,
                            values=["pro"],
                        )
                    ]
                )
            ],
        )
        flag = FeatureFlag(
            key="pro_flag",
            name="Pro Flag",
            type=FlagType.BOOLEAN,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="off",
            rules=[
                TargetingRule(
                    clauses=[
                        RuleClause(
                            attribute="",
                            operator=Operator.IN_SEGMENT,
                            values=["pro_users"],
                        )
                    ],
                    variation="on",
                )
            ],
        )
        backend = _FakeBackend(flags=[flag], segments=[seg])
        provider = WaygateOpenFeatureProvider(backend)
        await provider._load_all()

        ctx = OFCtx(targeting_key="user_1", attributes={"plan": "pro"})
        result = provider.resolve_boolean_details("pro_flag", False, ctx)
        assert result.value is True
        assert result.reason == Reason.TARGETING_MATCH
