"""Tests for WaygateFeatureClient and engine.use_openfeature()."""

from __future__ import annotations

from waygate.core.backends.memory import MemoryBackend
from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.client import WaygateFeatureClient
from waygate.core.feature_flags.models import (
    EvaluationContext,
    FeatureFlag,
    FlagType,
    FlagVariation,
)
from waygate.core.feature_flags.provider import WaygateOpenFeatureProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bool_flag(key="feat", fallthrough_variation="on", enabled=True) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Feat",
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough=fallthrough_variation,
        enabled=enabled,
    )


def _string_flag(key="color") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Color",
        type=FlagType.STRING,
        variations=[
            FlagVariation(name="blue", value="blue"),
            FlagVariation(name="red", value="red"),
        ],
        off_variation="blue",
        fallthrough="red",
    )


def _int_flag(key="limit") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Limit",
        type=FlagType.INTEGER,
        variations=[
            FlagVariation(name="low", value=10),
            FlagVariation(name="high", value=100),
        ],
        off_variation="low",
        fallthrough="high",
    )


def _float_flag(key="rate") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Rate",
        type=FlagType.FLOAT,
        variations=[
            FlagVariation(name="slow", value=0.1),
            FlagVariation(name="fast", value=0.9),
        ],
        off_variation="slow",
        fallthrough="fast",
    )


def _object_flag(key="cfg") -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="Config",
        type=FlagType.JSON,
        variations=[
            FlagVariation(name="default", value={"x": 1}),
            FlagVariation(name="premium", value={"x": 10}),
        ],
        off_variation="default",
        fallthrough="premium",
    )


class _FakeBackend:
    def __init__(self, flags=()):
        self._flags = list(flags)

    async def load_all_flags(self):
        return self._flags

    async def load_all_segments(self):
        return []

    # Minimal stubs so WaygateEngine can use this as backend
    async def startup(self):
        pass

    async def shutdown(self):
        pass

    async def subscribe_global_config(self):
        raise NotImplementedError

    async def subscribe_route_state(self):
        raise NotImplementedError

    async def subscribe_rate_limit_policies(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# engine.use_openfeature()
# ---------------------------------------------------------------------------


class TestUseOpenFeature:
    def test_returns_waygate_feature_client(self):
        engine = WaygateEngine()
        client = engine.use_openfeature(domain="test_uof")
        assert isinstance(client, WaygateFeatureClient)

    def test_flag_client_property_returns_client(self):
        engine = WaygateEngine()
        client = engine.use_openfeature(domain="test_prop")
        assert engine.flag_client is client

    def test_flag_client_none_before_use_openfeature(self):
        engine = WaygateEngine()
        assert engine.flag_client is None

    def test_custom_provider_accepted(self):
        engine = WaygateEngine()
        custom = WaygateOpenFeatureProvider(MemoryBackend())
        client = engine.use_openfeature(provider=custom, domain="test_custom")
        assert engine._flag_provider is custom
        assert isinstance(client, WaygateFeatureClient)

    def test_default_provider_is_waygate_provider(self):
        engine = WaygateEngine()
        engine.use_openfeature(domain="test_default_prov")
        assert isinstance(engine._flag_provider, WaygateOpenFeatureProvider)

    async def test_start_initializes_provider(self):
        initialized = []

        class _TrackedProvider(WaygateOpenFeatureProvider):
            def initialize(self, evaluation_context=None):
                initialized.append(True)

            def shutdown(self):
                pass

        engine = WaygateEngine()
        engine.use_openfeature(provider=_TrackedProvider(MemoryBackend()), domain="test_start")
        await engine.start()
        assert initialized == [True]
        await engine.stop()

    async def test_stop_shuts_down_provider(self):
        shutdown = []

        class _TrackedProvider(WaygateOpenFeatureProvider):
            def initialize(self, evaluation_context=None):
                pass

            def shutdown(self):
                shutdown.append(True)

        engine = WaygateEngine()
        engine.use_openfeature(provider=_TrackedProvider(MemoryBackend()), domain="test_stop")
        await engine.start()
        await engine.stop()
        assert shutdown == [True]

    def test_use_openfeature_multiple_calls_replaces_provider(self):
        engine = WaygateEngine()
        engine.use_openfeature(domain="test_multi_1")
        p1 = engine._flag_provider
        engine.use_openfeature(domain="test_multi_2")
        p2 = engine._flag_provider
        # Both are valid providers; second call replaced the first.
        assert p2 is not p1


# ---------------------------------------------------------------------------
# WaygateFeatureClient — evaluation
# ---------------------------------------------------------------------------


class TestWaygateFeatureClientEvaluation:
    async def _make_client(self, flags, domain) -> WaygateFeatureClient:
        """Wire up a provider with the given flags and return a client."""
        provider = WaygateOpenFeatureProvider(_FakeBackend(flags=flags))
        await provider._load_all()

        import openfeature.api as of_api

        of_api.set_provider(provider, domain=domain)
        return WaygateFeatureClient(domain=domain)

    async def test_get_boolean_value_true(self):
        client = await self._make_client([_bool_flag(fallthrough_variation="on")], "cli_bool")
        ctx = EvaluationContext(key="user_1")
        result = await client.get_boolean_value("feat", False, ctx)
        assert result is True

    async def test_get_boolean_value_missing_flag_returns_default(self):
        client = await self._make_client([], "cli_bool_miss")
        result = await client.get_boolean_value("missing", True)
        assert result is True

    async def test_get_string_value(self):
        client = await self._make_client([_string_flag()], "cli_str")
        ctx = EvaluationContext(key="user_1")
        result = await client.get_string_value("color", "default", ctx)
        assert result == "red"

    async def test_get_string_value_missing_flag(self):
        client = await self._make_client([], "cli_str_miss")
        result = await client.get_string_value("missing", "fallback")
        assert result == "fallback"

    async def test_get_integer_value(self):
        client = await self._make_client([_int_flag()], "cli_int")
        result = await client.get_integer_value("limit", 0)
        assert result == 100

    async def test_get_integer_value_missing(self):
        client = await self._make_client([], "cli_int_miss")
        result = await client.get_integer_value("limit", 99)
        assert result == 99

    async def test_get_float_value(self):
        client = await self._make_client([_float_flag()], "cli_float")
        result = await client.get_float_value("rate", 0.0)
        assert abs(result - 0.9) < 1e-9

    async def test_get_float_value_missing(self):
        client = await self._make_client([], "cli_float_miss")
        result = await client.get_float_value("rate", 1.5)
        assert abs(result - 1.5) < 1e-9

    async def test_get_object_value(self):
        client = await self._make_client([_object_flag()], "cli_obj")
        result = await client.get_object_value("cfg", {})
        assert result == {"x": 10}

    async def test_get_object_value_missing(self):
        client = await self._make_client([], "cli_obj_miss")
        result = await client.get_object_value("cfg", {"y": 2})
        assert result == {"y": 2}

    async def test_no_context_uses_anonymous(self):
        client = await self._make_client([_bool_flag()], "cli_anon")
        # Passing no context should not raise
        result = await client.get_boolean_value("feat", False)
        assert isinstance(result, bool)

    async def test_disabled_flag_returns_off_default(self):
        client = await self._make_client([_bool_flag(enabled=False)], "cli_dis")
        result = await client.get_boolean_value("feat", True)
        assert result is False  # off variation = False
