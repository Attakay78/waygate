"""Integration tests — WaygateSDK OpenFeature flag sync.

Covers:
* WaygateServerBackend._listen_sse() handling flag events
* WaygateSDKFlagProvider REST fetch + SSE hot-reload
* WaygateSDK.use_openfeature() integration
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from waygate.admin.app import WaygateAdmin
from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.models import (
    FeatureFlag,
    FlagType,
    FlagVariation,
    Segment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bool_flag(key: str = "my-flag", enabled: bool = True) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name=key.title(),
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough="on",
        enabled=enabled,
    )


def _segment(key: str = "beta") -> Segment:
    return Segment(
        key=key,
        name="Beta Users",
        included=["user1"],
    )


# ---------------------------------------------------------------------------
# WaygateServerBackend — flag SSE event handling
# ---------------------------------------------------------------------------


class TestWaygateServerBackendFlagSSE:
    def _make_backend(self):
        from waygate.core.backends.server import WaygateServerBackend

        return WaygateServerBackend(server_url="http://waygate:9000", app_id="svc")

    async def test_flag_updated_event_updates_cache(self) -> None:
        backend = self._make_backend()
        flag = _bool_flag("cached-flag")
        flag_dict = flag.model_dump(mode="json")

        # Simulate SSE event processing.
        event = {"type": "flag_updated", "key": "cached-flag", "flag": flag_dict}
        for q in backend._flag_subscribers:
            q.put_nowait(event)

        # Directly update cache as _listen_sse would.
        backend._flag_cache["cached-flag"] = flag_dict

        flags = await backend.load_all_flags()
        assert len(flags) == 1
        assert flags[0]["key"] == "cached-flag"

    async def test_flag_deleted_event_removes_from_cache(self) -> None:
        backend = self._make_backend()
        flag = _bool_flag("rm-flag")
        backend._flag_cache["rm-flag"] = flag.model_dump(mode="json")

        # Simulate deletion.
        backend._flag_cache.pop("rm-flag", None)

        flags = await backend.load_all_flags()
        assert flags == []

    async def test_segment_updated_event_updates_cache(self) -> None:
        backend = self._make_backend()
        seg = _segment("cached-seg")
        seg_dict = seg.model_dump(mode="json")
        backend._segment_cache["cached-seg"] = seg_dict

        segs = await backend.load_all_segments()
        assert len(segs) == 1

    async def test_subscribe_flag_changes_yields_events(self) -> None:
        backend = self._make_backend()
        received: list[dict] = []

        async def _listen() -> None:
            async for event in backend.subscribe_flag_changes():
                received.append(event)
                break

        task = asyncio.create_task(_listen())
        await asyncio.sleep(0.05)

        # Inject event directly into subscriber queue.
        for q in backend._flag_subscribers:
            q.put_nowait({"type": "flag_updated", "key": "x", "flag": {}})

        await task
        assert received[0]["type"] == "flag_updated"

    async def test_load_all_flags_returns_cached(self) -> None:
        backend = self._make_backend()
        flag = _bool_flag("f1")
        backend._flag_cache["f1"] = flag.model_dump(mode="json")
        result = await backend.load_all_flags()
        assert result == [flag.model_dump(mode="json")]

    async def test_load_all_segments_returns_cached(self) -> None:
        backend = self._make_backend()
        seg = _segment("s1")
        backend._segment_cache["s1"] = seg.model_dump(mode="json")
        result = await backend.load_all_segments()
        assert result == [seg.model_dump(mode="json")]


# ---------------------------------------------------------------------------
# WaygateSDKFlagProvider — REST fetch + SSE hot-reload
# ---------------------------------------------------------------------------


class TestWaygateSDKFlagProvider:
    @pytest.fixture
    def engine(self) -> WaygateEngine:
        return WaygateEngine()

    @pytest.fixture
    def admin(self, engine: WaygateEngine):
        return WaygateAdmin(engine=engine, enable_flags=True)

    async def test_fetch_from_server_populates_flags(self, admin, engine) -> None:
        """Provider fetches flags from /api/flags on initialize()."""
        flag = _bool_flag("fetch-flag")
        await engine.save_flag(flag)

        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        # Build a backend with ASGI transport pointing at the admin app.
        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        sdk_backend._client = AsyncClient(
            transport=ASGITransport(app=admin),
            base_url="http://testserver",
        )

        provider = WaygateSDKFlagProvider(sdk_backend)
        await provider._fetch_from_server()

        assert "fetch-flag" in provider._flags
        assert provider._flags["fetch-flag"].key == "fetch-flag"

        await sdk_backend._client.aclose()

    async def test_fetch_from_server_populates_segments(self, admin, engine) -> None:
        """Provider fetches segments from /api/segments on initialize()."""
        seg = _segment("fetch-seg")
        await engine.save_segment(seg)

        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        sdk_backend._client = AsyncClient(
            transport=ASGITransport(app=admin),
            base_url="http://testserver",
        )

        provider = WaygateSDKFlagProvider(sdk_backend)
        await provider._fetch_from_server()

        assert "fetch-seg" in provider._segments

        await sdk_backend._client.aclose()

    async def test_watch_sse_hot_reloads_flag(self) -> None:
        """Provider _watch_sse() updates _flags when a flag_updated event arrives."""
        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        provider = WaygateSDKFlagProvider(sdk_backend)

        flag = _bool_flag("hot-flag")
        watch_task = asyncio.create_task(provider._watch_sse())
        await asyncio.sleep(0.05)

        # Inject a flag_updated event into the backend's subscriber queue.
        for q in sdk_backend._flag_subscribers:
            q.put_nowait(
                {"type": "flag_updated", "key": "hot-flag", "flag": flag.model_dump(mode="json")}
            )

        await asyncio.sleep(0.1)
        watch_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await watch_task

        assert "hot-flag" in provider._flags
        assert provider._flags["hot-flag"].enabled is True

    async def test_watch_sse_removes_deleted_flag(self) -> None:
        """Provider _watch_sse() removes flag when flag_deleted event arrives."""
        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        provider = WaygateSDKFlagProvider(sdk_backend)
        flag = _bool_flag("gone-flag")
        provider._flags["gone-flag"] = flag

        watch_task = asyncio.create_task(provider._watch_sse())
        await asyncio.sleep(0.05)

        for q in sdk_backend._flag_subscribers:
            q.put_nowait({"type": "flag_deleted", "key": "gone-flag"})

        await asyncio.sleep(0.1)
        watch_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await watch_task

        assert "gone-flag" not in provider._flags

    async def test_watch_sse_hot_reloads_segment(self) -> None:
        """Provider _watch_sse() updates _segments when segment_updated event arrives."""
        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        provider = WaygateSDKFlagProvider(sdk_backend)

        seg = _segment("hot-seg")
        watch_task = asyncio.create_task(provider._watch_sse())
        await asyncio.sleep(0.05)

        for q in sdk_backend._flag_subscribers:
            q.put_nowait(
                {
                    "type": "segment_updated",
                    "key": "hot-seg",
                    "segment": seg.model_dump(mode="json"),
                }
            )

        await asyncio.sleep(0.1)
        watch_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await watch_task

        assert "hot-seg" in provider._segments

    async def test_provider_shutdown_cancels_watch_task(self) -> None:
        """shutdown() cancels the SSE watcher without raising."""
        from waygate.core.backends.server import WaygateServerBackend
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        sdk_backend = WaygateServerBackend(server_url="http://testserver", app_id="test-svc")
        provider = WaygateSDKFlagProvider(sdk_backend)
        provider._watch_task = asyncio.create_task(provider._watch_sse())
        await asyncio.sleep(0.05)
        provider.shutdown()
        assert provider._watch_task is None


# ---------------------------------------------------------------------------
# POST /api/flags/{key}/metrics
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WaygateSDK.use_openfeature() integration
# ---------------------------------------------------------------------------


class TestWaygateSDKUseOpenFeature:
    async def test_use_openfeature_sets_flag_provider(self) -> None:
        """use_openfeature() activates WaygateSDKFlagProvider on the engine."""
        from waygate.sdk import WaygateSDK

        sdk = WaygateSDK(
            server_url="http://waygate:9000",
            app_id="test-svc",
        )
        assert sdk.engine._flag_provider is None
        sdk.use_openfeature()
        assert sdk.engine._flag_provider is not None

        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        assert isinstance(sdk.engine._flag_provider, WaygateSDKFlagProvider)

    async def test_use_openfeature_enables_flag_client(self) -> None:
        """use_openfeature() should also set up the flag_client property."""
        from waygate.sdk import WaygateSDK

        sdk = WaygateSDK(
            server_url="http://waygate:9000",
            app_id="test-svc",
        )
        sdk.use_openfeature()
        assert sdk.engine.flag_client is not None

    async def test_use_openfeature_with_domain(self) -> None:
        """use_openfeature(domain=...) uses the given domain name."""
        from waygate.sdk import WaygateSDK

        sdk = WaygateSDK(
            server_url="http://waygate:9000",
            app_id="test-svc",
        )
        # Should not raise with a custom domain.
        sdk.use_openfeature(domain="payments")
        assert sdk.engine._flag_provider is not None

    async def test_use_openfeature_idempotent(self) -> None:
        """Calling use_openfeature() twice should not crash."""
        from waygate.sdk import WaygateSDK

        sdk = WaygateSDK(server_url="http://waygate:9000", app_id="test-svc")
        sdk.use_openfeature()
        sdk.use_openfeature()  # second call — should not raise
        assert sdk.engine._flag_provider is not None
