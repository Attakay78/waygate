"""WaygateSDKFlagProvider — OpenFeature provider for SDK clients.

Syncs feature flags and segments from a remote Waygate Server:

1. On ``initialize()`` it fetches all flags/segments via REST
   (``GET /api/flags`` and ``GET /api/segments``).
2. It then subscribes to the Waygate Server's SSE stream
   (``GET /api/sdk/events``) so any subsequent flag/segment mutations
   made on the server side are reflected locally with no polling.

Use ``WaygateSDK.use_openfeature()`` to activate this provider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from waygate.core.feature_flags._guard import _require_flags

_require_flags()

from waygate.core.feature_flags.models import FeatureFlag, Segment  # noqa: E402
from waygate.core.feature_flags.provider import WaygateOpenFeatureProvider  # noqa: E402

if TYPE_CHECKING:
    from waygate.core.backends.server import WaygateServerBackend

logger = logging.getLogger(__name__)

__all__ = ["WaygateSDKFlagProvider"]


class WaygateSDKFlagProvider(WaygateOpenFeatureProvider):
    """OpenFeature provider that hot-reloads flags from a Waygate Server.

    Parameters
    ----------
    backend:
        The :class:`~waygate.core.backends.server.WaygateServerBackend`
        instance used by this SDK — the same one passed to
        :class:`~waygate.sdk.WaygateSDK`.
    """

    def __init__(self, backend: WaygateServerBackend) -> None:
        super().__init__(backend)
        self._server_backend = backend
        self._watch_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # OpenFeature lifecycle
    # ------------------------------------------------------------------

    def initialize(self, evaluation_context: Any = None) -> None:
        """Fetch flags from the server and start the SSE watch task.

        The OpenFeature SDK calls this synchronously; async work is
        scheduled as asyncio tasks so no coroutine is left unawaited.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop — skip (unit-test or import-time context)
        loop.create_task(self._async_initialize(), name="waygate-sdk-flag-init")

    async def _async_initialize(self) -> None:
        await self._fetch_from_server()
        self._watch_task = asyncio.create_task(self._watch_sse(), name="waygate-sdk-flag-watch")

    def shutdown(self) -> None:
        """Cancel the SSE watcher task.

        The OpenFeature SDK calls this synchronously; the task is
        cancelled without awaiting so no coroutine is left unawaited.
        """
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_from_server(self) -> None:
        """Pull current flags and segments from the Waygate Server REST API."""
        client = self._server_backend._client
        if client is None:
            logger.warning(
                "WaygateSDKFlagProvider: HTTP client not ready — skipping initial flag fetch"
            )
            return

        try:
            resp = await client.get("/api/flags")
            if resp.status_code == 200:
                data = resp.json()
                # The API returns either a list directly or {"flags": [...]}
                items = data if isinstance(data, list) else data.get("flags", [])
                for item in items:
                    try:
                        flag = FeatureFlag.model_validate(item)
                        self._flags[flag.key] = flag
                        # Also populate the backend's raw cache so
                        # load_all_flags() returns the right data.
                        self._server_backend._flag_cache[flag.key] = item
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            logger.warning("WaygateSDKFlagProvider: could not fetch flags from server")

        try:
            resp = await client.get("/api/segments")
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("segments", [])
                for item in items:
                    try:
                        seg = Segment.model_validate(item)
                        self._segments[seg.key] = seg
                        self._server_backend._segment_cache[seg.key] = item
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            logger.warning("WaygateSDKFlagProvider: could not fetch segments from server")

        logger.info(
            "WaygateSDKFlagProvider: loaded %d flag(s), %d segment(s) from server",
            len(self._flags),
            len(self._segments),
        )

    async def _watch_sse(self) -> None:
        """Subscribe to the backend's flag change queue and update local cache."""
        try:
            async for event in self._server_backend.subscribe_flag_changes():
                etype = event.get("type")

                if etype == "flag_updated":
                    raw = event.get("flag")
                    if raw is not None:
                        try:
                            flag = FeatureFlag.model_validate(raw)
                            self._flags[flag.key] = flag
                            logger.debug("WaygateSDKFlagProvider: flag hot-reloaded — %s", flag.key)
                        except Exception:  # noqa: BLE001
                            pass

                elif etype == "flag_deleted":
                    key = event.get("key", "")
                    self._flags.pop(key, None)
                    logger.debug("WaygateSDKFlagProvider: flag removed — %s", key)

                elif etype == "segment_updated":
                    raw = event.get("segment")
                    if raw is not None:
                        try:
                            seg = Segment.model_validate(raw)
                            self._segments[seg.key] = seg
                            logger.debug(
                                "WaygateSDKFlagProvider: segment hot-reloaded — %s", seg.key
                            )
                        except Exception:  # noqa: BLE001
                            pass

                elif etype == "segment_deleted":
                    key = event.get("key", "")
                    self._segments.pop(key, None)
                    logger.debug("WaygateSDKFlagProvider: segment removed — %s", key)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WaygateSDKFlagProvider: SSE watch error")
