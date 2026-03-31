"""Redis backend for waygate.

Uses ``redis.asyncio`` for all I/O.  Supports multi-instance deployments
via pub/sub on the ``waygate:changes`` channel.

Key schema
----------
``waygate:state:{path}``        — JSON-serialized ``RouteState``
``waygate:route-index``         — Redis Set of all registered route paths
                                  (replaces dangerous ``KEYS`` scans with safe
                                  O(N) ``SMEMBERS`` that does not block the server)
``waygate:audit``               — Redis list, newest-first (LPUSH + LTRIM to 1000)
``waygate:audit:path:{path}``   — Per-path audit list for O(limit) filtered queries
                                  instead of fetching all 1000 entries to filter in Python
``waygate:changes``             — pub/sub channel for live state updates

Performance notes
-----------------
*  ``list_states()`` uses ``SMEMBERS waygate:route-index`` + ``MGET`` instead
   of ``KEYS waygate:state:*``.  ``KEYS`` is an O(keyspace) blocking command
   that freezes Redis on production instances; ``SMEMBERS`` on a dedicated
   set is safe and equally fast.
*  ``set_state()`` / ``delete_state()`` maintain the route-index atomically
   via pipeline so the set and the state key are always in sync.
*  ``get_audit_log(path=X)`` reads directly from ``waygate:audit:path:X``
   instead of fetching up to 1000 global entries and filtering in Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool

from waygate.core.backends.base import WaygateBackend
from waygate.core.models import AuditEntry, GlobalMaintenanceConfig, RouteState

if TYPE_CHECKING:
    from waygate.core.rate_limit.models import RateLimitHit

logger = logging.getLogger(__name__)

_AUDIT_KEY = "waygate:audit"
_RATE_LIMIT_HITS_KEY = "waygate:ratelimit:hits"
_ROUTE_INDEX_KEY = "waygate:route-index"
_CHANGES_CHANNEL = "waygate:changes"
# Lightweight pub/sub channel used exclusively for cross-instance global
# config cache invalidation.  Publishing a signal here tells every other
# instance to drop its in-process GlobalMaintenanceConfig cache and
# re-fetch from Redis on the next request.  The payload is always "1" —
# only the arrival of the message matters, not its content.
_GLOBAL_INVALIDATE_CHANNEL = "waygate:global_invalidate"
_RL_POLICY_CHANNEL = "waygate:rl-policy-change"
_MAX_AUDIT_ENTRIES = 1000


def _state_key(path: str) -> str:
    return f"waygate:state:{path}"


def _audit_path_key(path: str) -> str:
    """Per-path audit list key for O(limit) filtered audit queries."""
    return f"waygate:audit:path:{path}"


class RedisBackend(WaygateBackend):
    """Backend that stores all state in Redis.

    Supports multi-instance deployments.  ``subscribe()`` uses Redis
    pub/sub so that state changes made by one instance are immediately
    visible in the dashboard on any other instance.

    Parameters
    ----------
    url:
        Redis connection URL (e.g. ``"redis://localhost:6379/0"``).
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        max_rl_hit_entries: int = 10_000,
    ) -> None:
        self._url = url
        self._max_rl_hit_entries = max_rl_hit_entries
        # One ConnectionPool per event loop, keyed by id(loop).
        # Values are (weakref.ref(loop), pool) — a weak reference so the loop
        # can be GC'd naturally when replaced, and the pool alongside it.
        self._pools: dict[int, tuple[weakref.ref[asyncio.AbstractEventLoop], ConnectionPool]] = {}

    def _get_pool(self) -> ConnectionPool:
        """Return a ConnectionPool bound to the current running event loop.

        A new pool is created whenever the running loop differs from the one
        the existing pool was created under — this handles gunicorn worker
        recycles and uvicorn ``--reload`` restarts where the event loop is
        replaced mid-process without a fresh interpreter.

        Dead-loop pruning runs only when a new pool must be created (the rare
        event of a loop replacement), keeping the hot path to a single dict
        lookup.
        """
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        cached = self._pools.get(loop_id)
        if cached is not None:
            loop_ref, pool = cached
            if loop_ref() is loop:
                # Same loop still running — reuse the pool.
                return pool
            # Either the weak ref is dead (loop was GC'd and id reused) or
            # the id belongs to a different live loop. Either way, discard.
            del self._pools[loop_id]

        # Creating a new pool: prune any entries whose loop has been GC'd.
        # This is O(n) over a tiny dict (at most a handful of entries across
        # the lifetime of a process) and only runs on the rare loop-replacement
        # event, never on the hot request path.
        dead = [lid for lid, (ref, _) in self._pools.items() if ref() is None]
        for lid in dead:
            del self._pools[lid]

        pool = ConnectionPool.from_url(self._url, decode_responses=True)
        self._pools[loop_id] = (weakref.ref(loop), pool)
        return pool

    def _client(self) -> aioredis.Redis:
        """Return a Redis client bound to the current event loop's pool."""
        return aioredis.Redis(connection_pool=self._get_pool())

    # ------------------------------------------------------------------
    # WaygateBackend interface
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path*.

        Raises ``KeyError`` if no state has been registered for *path*.
        """
        try:
            async with self._client() as r:
                raw = await r.get(_state_key(path))
        except Exception as exc:
            logger.error("waygate: redis get_state error for %r: %s", path, exc)
            raise

        if raw is None:
            raise KeyError(f"No state registered for path {path!r}")
        return RouteState.model_validate(json.loads(raw))

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path*, update the route-index, and publish to
        ``waygate:changes``.

        The state key and the route-index entry are written atomically in a
        single pipeline so ``list_states()`` can never see a state key that
        is missing from the index (or vice-versa).
        """
        payload = state.model_dump_json()
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.set(_state_key(path), payload)
                pipe.sadd(_ROUTE_INDEX_KEY, path)
                pipe.publish(_CHANGES_CHANNEL, payload)
                await pipe.execute()
        except Exception as exc:
            logger.error("waygate: redis set_state error for %r: %s", path, exc)
            raise

    async def delete_state(self, path: str) -> None:
        """Remove state for *path* and remove it from the route-index.

        No-op if *path* is not registered.
        """
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.delete(_state_key(path))
                pipe.srem(_ROUTE_INDEX_KEY, path)
                await pipe.execute()
        except Exception as exc:
            logger.error("waygate: redis delete_state error: %s", exc)
            raise

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states.

        Uses ``SMEMBERS waygate:route-index`` + ``MGET`` instead of the
        dangerous ``KEYS waygate:state:*`` pattern.  ``KEYS`` is an O(keyspace)
        blocking command that can freeze a busy Redis server; ``SMEMBERS`` on
        the dedicated route-index set is safe to use in production.
        """
        try:
            async with self._client() as r:
                paths: set[str] = await r.smembers(_ROUTE_INDEX_KEY)  # type: ignore[misc]
                if not paths:
                    return []
                keys = [_state_key(p) for p in paths]
                values: list[str | None] = await r.mget(*keys)
        except Exception as exc:
            logger.error("waygate: redis list_states error: %s", exc)
            raise

        states: list[RouteState] = []
        for raw in values:
            if raw is not None:
                states.append(RouteState.model_validate(json.loads(raw)))
        return states

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to both the global audit list and the per-path list.

        Both lists are capped at 1000 entries via ``LTRIM``.  Writing to a
        per-path list means ``get_audit_log(path=X)`` can fetch exactly the
        required entries directly — no full-list fetch-then-filter in Python.
        """
        payload = entry.model_dump_json()
        path_key = _audit_path_key(entry.path)
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                # Global audit list (for unfiltered queries).
                pipe.lpush(_AUDIT_KEY, payload)
                pipe.ltrim(_AUDIT_KEY, 0, _MAX_AUDIT_ENTRIES - 1)
                # Per-path audit list (for filtered queries — O(limit) instead
                # of O(1000) fetch-then-filter).
                pipe.lpush(path_key, payload)
                pipe.ltrim(path_key, 0, _MAX_AUDIT_ENTRIES - 1)
                await pipe.execute()
        except Exception as exc:
            logger.error("waygate: redis write_audit error: %s", exc)
            raise

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first.

        When *path* is provided the per-path list is used — fetches exactly
        *limit* entries via a single ``LRANGE`` call, eliminating the
        fetch-all-then-filter pattern of the previous implementation.
        """
        try:
            async with self._client() as r:
                if path is not None:
                    # Per-path list: fetch exactly what we need — O(limit).
                    raws: list[str] = await r.lrange(  # type: ignore[misc]
                        _audit_path_key(path), 0, limit - 1
                    )
                else:
                    # Global list: all entries newest-first.
                    raws = await r.lrange(_AUDIT_KEY, 0, limit - 1)  # type: ignore[misc]
        except Exception as exc:
            logger.error("waygate: redis get_audit_log error: %s", exc)
            raise

        return [AuditEntry.model_validate(json.loads(raw)) for raw in raws]

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Yield ``RouteState`` objects as they are updated via pub/sub."""
        async with self._client() as r:
            async with r.pubsub() as pubsub:
                await pubsub.subscribe(_CHANGES_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        state = RouteState.model_validate(json.loads(message["data"]))
                        yield state
                    except Exception as exc:
                        logger.warning("waygate: redis subscribe parse error: %s", exc)

    # ------------------------------------------------------------------
    # Distributed global config — override for cross-instance cache invalidation
    # ------------------------------------------------------------------

    async def set_global_config(self, config: GlobalMaintenanceConfig) -> None:
        """Persist *config* and broadcast a cache-invalidation signal.

        Calls the base implementation (which stores the config via
        ``set_state`` and publishes to ``waygate:changes``), then
        additionally publishes a lightweight ``"1"`` signal to
        ``waygate:global_invalidate``.  Any other instance running
        ``subscribe_global_config()`` receives this signal and
        immediately drops its in-process ``GlobalMaintenanceConfig``
        cache so that the next ``check()`` call re-reads from Redis.

        The extra publish is best-effort: a Redis error here is logged
        and swallowed so that a transient Redis blip never prevents
        a global maintenance toggle from taking effect locally.
        """
        await super().set_global_config(config)
        try:
            async with self._client() as r:
                await r.publish(_GLOBAL_INVALIDATE_CHANNEL, "1")
        except Exception as exc:
            logger.warning("waygate: failed to publish global config invalidation signal: %s", exc)

    async def try_claim_webhook_dispatch(self, dedup_key: str, ttl_seconds: int = 60) -> bool:
        """Use Redis ``SET NX`` to claim exclusive webhook dispatch rights.

        The key ``waygate:webhook:dedup:{dedup_key}`` is written with
        ``NX`` (only if absent) and a TTL.  The instance that wins the
        atomic write fires the webhooks; all others receive ``None`` from
        Redis and skip dispatch.

        Fails open: a Redis error logs a warning and returns ``True`` so
        that webhooks are over-delivered rather than silently dropped.

        Parameters
        ----------
        dedup_key:
            Deterministic key identifying the event (hash of event + path
            + serialised state — computed by ``WaygateEngine``).
        ttl_seconds:
            Key TTL.  After this window the key expires, allowing
            re-delivery if the winning instance crashed mid-dispatch.
        """
        redis_key = f"waygate:webhook:dedup:{dedup_key}"
        try:
            async with self._client() as r:
                # SET NX returns the string "OK" when the key was written,
                # None when the key already existed.
                result = await r.set(redis_key, "1", nx=True, ex=ttl_seconds)
            return result is not None
        except Exception as exc:
            logger.warning("waygate: redis webhook dedup check failed (%s) — failing open", exc)
            return True  # fail-open: over-deliver rather than miss

    async def write_rate_limit_hit(self, hit: RateLimitHit) -> None:
        """Append a rate limit hit record, evicting the oldest when the cap is reached."""
        payload = hit.model_dump_json()
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.lpush(_RATE_LIMIT_HITS_KEY, payload)
                pipe.ltrim(_RATE_LIMIT_HITS_KEY, 0, self._max_rl_hit_entries - 1)
                await pipe.execute()
        except Exception as exc:
            logger.warning("waygate: redis write_rate_limit_hit error: %s", exc)

    async def get_rate_limit_hits(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[RateLimitHit]:
        """Return rate limit hits, newest first, optionally filtered by *path*."""
        from waygate.core.rate_limit.models import RateLimitHit as RLHit

        try:
            async with self._client() as r:
                raws: list[str] = await r.lrange(_RATE_LIMIT_HITS_KEY, 0, -1)  # type: ignore[misc]
        except Exception as exc:
            logger.warning("waygate: redis get_rate_limit_hits error: %s", exc)
            return []

        hits: list[RLHit] = []
        for raw in raws:
            try:
                hit = RLHit.model_validate(json.loads(raw))
                if path is None or hit.path == path:
                    hits.append(hit)
                    if len(hits) >= limit:
                        break
            except Exception:
                continue
        return hits

    async def set_rate_limit_policy(
        self, path: str, method: str, policy_data: dict[str, Any]
    ) -> None:
        """Persist *policy_data* for *path*/*method* in Redis and broadcast to
        all other instances via ``waygate:rl-policy-change``."""
        key = f"{method.upper()}:{path}"
        redis_key = f"waygate:rlpolicy:{key}"
        index_key = "waygate:rl-policy-index"
        event = json.dumps({"action": "set", "key": key, "policy": policy_data})
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.set(redis_key, json.dumps(policy_data))
                pipe.sadd(index_key, key)
                pipe.publish(_RL_POLICY_CHANNEL, event)
                await pipe.execute()
        except Exception as exc:
            logger.warning("waygate: redis set_rate_limit_policy error: %s", exc)

    async def get_rate_limit_policies(self) -> list[dict[str, Any]]:
        """Return all persisted rate limit policies from Redis."""
        index_key = "waygate:rl-policy-index"
        try:
            async with self._client() as r:
                keys: set[str] = await r.smembers(index_key)  # type: ignore[misc]
                if not keys:
                    return []
                redis_keys = [f"waygate:rlpolicy:{k}" for k in keys]
                raws: list[str | None] = await r.mget(*redis_keys)
            policies = []
            for raw in raws:
                if raw is not None:
                    try:
                        policies.append(json.loads(raw))
                    except Exception:
                        continue
            return policies
        except Exception as exc:
            logger.warning("waygate: redis get_rate_limit_policies error: %s", exc)
            return []

    async def delete_rate_limit_policy(self, path: str, method: str) -> None:
        """Remove the persisted rate limit policy for *path*/*method* from Redis
        and broadcast the deletion to all other instances."""
        key = f"{method.upper()}:{path}"
        redis_key = f"waygate:rlpolicy:{key}"
        index_key = "waygate:rl-policy-index"
        event = json.dumps({"action": "delete", "key": key})
        try:
            async with self._client() as r:
                pipe = r.pipeline()
                pipe.delete(redis_key)
                pipe.srem(index_key, key)
                pipe.publish(_RL_POLICY_CHANNEL, event)
                await pipe.execute()
        except Exception as exc:
            logger.warning("waygate: redis delete_rate_limit_policy error: %s", exc)

    async def subscribe_global_config(self) -> AsyncIterator[None]:
        """Yield ``None`` whenever the global maintenance config changes.

        Subscribes to ``waygate:global_invalidate``.  Each message
        arrival means another instance has written a new
        ``GlobalMaintenanceConfig`` to Redis and this instance should
        drop its in-process cache.

        The generator runs indefinitely — callers (``WaygateEngine``)
        are expected to run it inside a cancellable ``asyncio.Task``.
        """
        async with self._client() as r:
            async with r.pubsub() as pubsub:
                await pubsub.subscribe(_GLOBAL_INVALIDATE_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    yield None

    async def subscribe_rate_limit_policy(self) -> AsyncIterator[dict[str, Any]]:
        """Yield policy-change events whenever another instance sets or deletes
        a rate limit policy.

        Each yielded dict has one of two shapes::

            {"action": "set",    "key": "GET:/api/orders", "policy": {...}}
            {"action": "delete", "key": "GET:/api/orders"}

        The generator runs indefinitely inside a cancellable ``asyncio.Task``
        managed by ``WaygateEngine``.
        """
        async with self._client() as r:
            async with r.pubsub() as pubsub:
                await pubsub.subscribe(_RL_POLICY_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        yield json.loads(message["data"])
                    except Exception as exc:
                        logger.warning("waygate: redis rl-policy-change parse error: %s", exc)
