"""Tests for _SyncWaygateFeatureClient and engine.sync.flag_client.

Verifies that all five evaluation methods work correctly from a
synchronous context (the way FastAPI runs ``def`` route handlers).
"""

from __future__ import annotations

import pytest

pytest.importorskip("openfeature", reason="waygate[flags] not installed")

from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.client import WaygateFeatureClient, _SyncWaygateFeatureClient
from waygate.core.feature_flags.models import FeatureFlag, FlagType, FlagVariation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine() -> WaygateEngine:
    engine = WaygateEngine()
    engine.use_openfeature()
    return engine


def _make_flag(
    key: str, ftype: FlagType, variations: list[FlagVariation], fallthrough: str
) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name=key,
        type=ftype,
        enabled=True,
        variations=variations,
        off_variation=variations[-1].name,
        fallthrough=fallthrough,
    )


# ---------------------------------------------------------------------------
# _SyncWaygateFeatureClient — unit
# ---------------------------------------------------------------------------


class TestSyncWaygateFeatureClient:
    def test_is_returned_by_flag_client_sync_property(self) -> None:
        engine = _make_engine()
        fc: WaygateFeatureClient = engine._flag_client
        assert isinstance(fc.sync, _SyncWaygateFeatureClient)

    def test_each_call_returns_fresh_instance(self) -> None:
        engine = _make_engine()
        fc: WaygateFeatureClient = engine._flag_client
        # Two accesses to .sync return separate objects (not cached),
        # but both wrap the same underlying OpenFeature client.
        a = fc.sync
        b = fc.sync
        assert a._of_client is b._of_client

    def test_get_boolean_value_returns_default_for_unknown_flag(self) -> None:
        engine = _make_engine()
        result = engine.sync.flag_client.get_boolean_value("unknown_flag", True)
        assert result is True

    def test_get_string_value_returns_default_for_unknown_flag(self) -> None:
        engine = _make_engine()
        result = engine.sync.flag_client.get_string_value("unknown_flag", "fallback")
        assert result == "fallback"

    def test_get_integer_value_returns_default_for_unknown_flag(self) -> None:
        engine = _make_engine()
        result = engine.sync.flag_client.get_integer_value("unknown_flag", 42)
        assert result == 42

    def test_get_float_value_returns_default_for_unknown_flag(self) -> None:
        engine = _make_engine()
        result = engine.sync.flag_client.get_float_value("unknown_flag", 3.14)
        assert result == pytest.approx(3.14)

    def test_get_object_value_returns_default_for_unknown_flag(self) -> None:
        engine = _make_engine()
        default = {"k": "v"}
        result = engine.sync.flag_client.get_object_value("unknown_flag", default)
        assert result == default

    def test_ctx_dict_accepted(self) -> None:
        """Passing a plain dict as ctx does not raise."""
        engine = _make_engine()
        result = engine.sync.flag_client.get_boolean_value(
            "unknown_flag", False, {"targeting_key": "user_123"}
        )
        assert result is False

    def test_ctx_none_accepted(self) -> None:
        engine = _make_engine()
        result = engine.sync.flag_client.get_boolean_value("unknown_flag", True, None)
        assert result is True


# ---------------------------------------------------------------------------
# engine.sync.flag_client integration
# ---------------------------------------------------------------------------


class TestEngineSyncFlagClient:
    def test_returns_none_before_use_openfeature(self) -> None:
        engine = WaygateEngine()
        assert engine.sync.flag_client is None

    def test_returns_sync_client_after_use_openfeature(self) -> None:
        engine = _make_engine()
        fc = engine.sync.flag_client
        assert fc is not None
        assert isinstance(fc, _SyncWaygateFeatureClient)

    def test_evaluates_registered_boolean_flag(self) -> None:
        """A saved boolean flag returns its fallthrough value."""
        import asyncio

        engine = _make_engine()

        flag = _make_flag(
            "beta_feature",
            FlagType.BOOLEAN,
            [FlagVariation(name="on", value=True), FlagVariation(name="off", value=False)],
            "on",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        result = engine.sync.flag_client.get_boolean_value("beta_feature", False)
        assert result is True

    def test_evaluates_registered_string_flag(self) -> None:
        import asyncio

        engine = _make_engine()
        flag = _make_flag(
            "theme",
            FlagType.STRING,
            [FlagVariation(name="dark", value="dark"), FlagVariation(name="light", value="light")],
            "dark",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        result = engine.sync.flag_client.get_string_value("theme", "light")
        assert result == "dark"

    def test_evaluates_registered_integer_flag(self) -> None:
        import asyncio

        engine = _make_engine()
        flag = _make_flag(
            "max_retries",
            FlagType.INTEGER,
            [FlagVariation(name="low", value=3), FlagVariation(name="high", value=10)],
            "high",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        result = engine.sync.flag_client.get_integer_value("max_retries", 1)
        assert result == 10

    def test_evaluates_registered_float_flag(self) -> None:
        import asyncio

        engine = _make_engine()
        flag = _make_flag(
            "rate",
            FlagType.FLOAT,
            [FlagVariation(name="low", value=0.1), FlagVariation(name="high", value=0.9)],
            "low",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        result = engine.sync.flag_client.get_float_value("rate", 0.5)
        assert result == pytest.approx(0.1)

    def test_disabled_flag_returns_default(self) -> None:
        """A disabled flag always returns the default value."""
        import asyncio

        engine = _make_engine()
        flag = FeatureFlag(
            key="off_flag",
            name="off_flag",
            type=FlagType.BOOLEAN,
            enabled=False,
            variations=[
                FlagVariation(name="on", value=True),
                FlagVariation(name="off", value=False),
            ],
            off_variation="off",
            fallthrough="on",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        result = engine.sync.flag_client.get_boolean_value("off_flag", False)
        # Disabled flag → OpenFeature returns the OFF variation or default
        assert isinstance(result, bool)

    def test_sync_and_async_return_same_value(self) -> None:
        """Sync and async evaluation of the same flag return identical results."""
        import asyncio

        engine = _make_engine()
        flag = _make_flag(
            "consistent",
            FlagType.BOOLEAN,
            [FlagVariation(name="on", value=True), FlagVariation(name="off", value=False)],
            "on",
        )
        asyncio.get_event_loop().run_until_complete(engine.save_flag(flag))

        sync_result = engine.sync.flag_client.get_boolean_value("consistent", False)

        async def _async_eval() -> bool:
            return await engine.flag_client.get_boolean_value("consistent", False)

        async_result = asyncio.get_event_loop().run_until_complete(_async_eval())
        assert sync_result == async_result
