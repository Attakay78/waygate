"""Redis backend for api-shield.

Uses ``redis.asyncio`` for all I/O.  Supports multi-instance deployments
via pub/sub on the ``shield:changes`` channel.

Key schema
----------
``shield:state:{path}``   — JSON-serialized ``RouteState``
``shield:audit``           — Redis list, newest-first (LPUSH + LTRIM to 1000)
``shield:changes``         — pub/sub channel for live state updates
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

logger = logging.getLogger(__name__)

_AUDIT_KEY = "shield:audit"
_CHANGES_CHANNEL = "shield:changes"
_MAX_AUDIT_ENTRIES = 1000


def _state_key(path: str) -> str:
    return f"shield:state:{path}"


class RedisBackend(ShieldBackend):
    """Backend that stores all state in Redis.

    Supports multi-instance deployments.  ``subscribe()`` uses Redis
    pub/sub so that state changes made by one instance are immediately
    visible in the dashboard on any other instance.

    Parameters
    ----------
    url:
        Redis connection URL (e.g. ``"redis://localhost:6379/0"``).
    """

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._pool = ConnectionPool.from_url(url, decode_responses=True)

    def _client(self) -> aioredis.Redis:
        """Return a Redis client using the shared connection pool."""
        return aioredis.Redis(connection_pool=self._pool)

    # ------------------------------------------------------------------
    # ShieldBackend interface
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path*.

        Raises ``KeyError`` if no state has been registered for *path*.
        """
        try:
            async with self._client() as r:
                raw = await r.get(_state_key(path))
        except Exception as exc:
            logger.error("shield: redis get_state error for %r: %s", path, exc)
            raise

        if raw is None:
            raise KeyError(f"No state registered for path {path!r}")
        return RouteState.model_validate(json.loads(raw))

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path* and publish to ``shield:changes``."""
        payload = state.model_dump_json()
        try:
            async with self._client() as r:
                await r.set(_state_key(path), payload)
                await r.publish(_CHANGES_CHANNEL, payload)
        except Exception as exc:
            logger.error("shield: redis set_state error for %r: %s", path, exc)
            raise

    async def delete_state(self, path: str) -> None:
        """Remove state for *path*. No-op if not registered."""
        try:
            async with self._client() as r:
                await r.delete(_state_key(path))
        except Exception as exc:
            logger.error("shield: redis delete_state error: %s", exc)
            raise

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        try:
            async with self._client() as r:
                keys: list[str] = await r.keys("shield:state:*")
                if not keys:
                    return []
                values: list[str | None] = await r.mget(*keys)
        except Exception as exc:
            logger.error("shield: redis list_states error: %s", exc)
            raise

        states: list[RouteState] = []
        for raw in values:
            if raw is not None:
                states.append(RouteState.model_validate(json.loads(raw)))
        return states

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the Redis audit list (capped at 1000)."""
        payload = entry.model_dump_json()
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.lpush(_AUDIT_KEY, payload)
                pipe.ltrim(_AUDIT_KEY, 0, _MAX_AUDIT_ENTRIES - 1)
                await pipe.execute()
        except Exception as exc:
            logger.error("shield: redis write_audit error: %s", exc)
            raise

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*."""
        try:
            async with self._client() as r:
                # Fetch more than limit to allow post-filter narrowing.
                fetch = limit if path is None else _MAX_AUDIT_ENTRIES
                raws: list[str] = await r.lrange(_AUDIT_KEY, 0, fetch - 1)
        except Exception as exc:
            logger.error("shield: redis get_audit_log error: %s", exc)
            raise

        entries: list[AuditEntry] = []
        for raw in raws:
            entry = AuditEntry.model_validate(json.loads(raw))
            if path is None or entry.path == path:
                entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Yield ``RouteState`` objects as they are updated via pub/sub."""
        async with self._client() as r:
            async with r.pubsub() as pubsub:
                await pubsub.subscribe(_CHANGES_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        state = RouteState.model_validate(
                            json.loads(message["data"])
                        )
                        yield state
                    except Exception as exc:
                        logger.warning(
                            "shield: redis subscribe parse error: %s", exc
                        )
