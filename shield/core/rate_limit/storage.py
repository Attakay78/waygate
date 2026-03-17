"""Rate limit storage bridge — wraps the ``limits`` library backends.

The ``limits`` library handles all counter arithmetic, window management,
and expiry.  This module wires it to api-shield's backend infrastructure.

Storage hierarchy
-----------------
``RateLimitStorage`` (ABC)
  ├── ``MemoryRateLimitStorage``  — wraps ``limits.MemoryStorage``
  ├── ``RedisRateLimitStorage``   — wraps ``limits.RedisStorage``
  └── ``FileRateLimitStorage``    — in-memory counters + periodic JSON snapshot

All three are created via ``create_rate_limit_storage(backend)`` which
auto-selects the appropriate storage based on the active ``ShieldBackend``.

Import guard
------------
Every public symbol in this module is safe to import without ``limits``
installed — ``HAS_LIMITS`` is False and any attempt to *use* the classes
will raise ``ImportError`` with a clear install instruction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import limits as _limits_lib  # noqa: F401

    HAS_LIMITS = True
except ImportError:
    HAS_LIMITS = False

if TYPE_CHECKING:
    from shield.core.backends.base import ShieldBackend

from shield.core.rate_limit.models import RateLimitAlgorithm, RateLimitResult

logger = logging.getLogger(__name__)

# How long before the window ends to report 0 remaining (avoids negative).
_RATE_LIMIT_NAMESPACE = "shield:ratelimit"


class RateLimitStorage(ABC):
    """Abstract base for rate limit counter storage.

    Implementations wrap a concrete ``limits`` storage backend and translate
    its API into the simpler interface that ``ShieldRateLimiter`` needs.
    """

    @abstractmethod
    async def increment(
        self,
        key: str,
        limit: str,
        algorithm: RateLimitAlgorithm,
    ) -> RateLimitResult:
        """Increment the counter for *key* and return the check result.

        Parameters
        ----------
        key:
            Fully-namespaced rate limit key (e.g.
            ``"shield:ratelimit:GET:/api/payments:192.168.1.1"``).
        limit:
            Limit string in ``limits`` format, e.g. ``"100/minute"``.
        algorithm:
            Which counting algorithm to apply.
        """

    @abstractmethod
    async def get_remaining(self, key: str, limit: str) -> int:
        """Return the number of requests remaining in the current window."""

    @abstractmethod
    async def reset(self, key: str) -> None:
        """Clear the counter for the specific namespaced *key*."""

    @abstractmethod
    async def reset_all_for_path(self, path: str) -> None:
        """Clear all counters whose keys contain *path*."""

    async def startup(self) -> None:
        """Called once after the storage is created, in an async context.

        Override to kick off background tasks (e.g. the periodic snapshot
        writer in ``FileRateLimitStorage``).  Default is a no-op.
        """

    async def shutdown(self) -> None:
        """Called on ASGI lifespan shutdown.  Default is a no-op."""


def _get_strategy(algorithm: RateLimitAlgorithm, storage: Any) -> Any:
    """Return the ``limits`` strategy instance for *algorithm*."""
    from limits.strategies import (
        FixedWindowRateLimiter,
        MovingWindowRateLimiter,
        SlidingWindowCounterRateLimiter,
    )

    mapping: dict[RateLimitAlgorithm, type[Any]] = {
        RateLimitAlgorithm.FIXED_WINDOW: FixedWindowRateLimiter,
        RateLimitAlgorithm.SLIDING_WINDOW: SlidingWindowCounterRateLimiter,
        RateLimitAlgorithm.MOVING_WINDOW: MovingWindowRateLimiter,
        # TOKEN_BUCKET → MovingWindowRateLimiter (closest approximation in limits v3)
        RateLimitAlgorithm.TOKEN_BUCKET: MovingWindowRateLimiter,
    }
    cls = mapping[algorithm]
    return cls(storage)


def _build_result(
    *,
    key: str,
    limit_str: str,
    allowed: bool,
    remaining: int,
    reset_at: datetime,
    tier: str | None = None,
    key_was_missing: bool = False,
    missing_key_behaviour: Any = None,
) -> RateLimitResult:
    now = datetime.now(UTC)
    retry_after = max(0, int((reset_at - now).total_seconds())) if not allowed else 0
    return RateLimitResult(
        allowed=allowed,
        limit=limit_str,
        remaining=max(0, remaining),
        reset_at=reset_at,
        retry_after_seconds=retry_after,
        key=key,
        tier=tier,
        key_was_missing=key_was_missing,
        missing_key_behaviour=missing_key_behaviour,
    )


class MemoryRateLimitStorage(RateLimitStorage):
    """In-process rate limit storage wrapping ``limits.MemoryStorage``.

    Thread-safe via ``asyncio.Lock``.  **Single-process only** — each worker
    has its own independent counter.  For multi-worker deployments use
    ``RedisRateLimitStorage``.

    Parameters
    ----------
    default_algorithm:
        Which counting algorithm to use when ``increment()`` is called
        without a specific override.
    """

    def __init__(
        self,
        default_algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW,
    ) -> None:
        if not HAS_LIMITS:
            raise ImportError(
                "Rate limiting requires the 'limits' library. "
                "Install it with: pip install api-shield[rate-limit]"
            )
        from limits.storage import MemoryStorage

        self._mem_storage = MemoryStorage()
        self._lock = asyncio.Lock()
        self._default_algorithm = default_algorithm
        # Cache strategy instances (one per algorithm) to avoid re-construction.
        self._strategies: dict[RateLimitAlgorithm, Any] = {}
        logger.debug(
            "shield: MemoryRateLimitStorage initialised. "
            "NOTE: not safe for multi-worker deployments — counters are per-process."
        )

    def _get_strategy(self, algorithm: RateLimitAlgorithm) -> Any:
        if algorithm not in self._strategies:
            self._strategies[algorithm] = _get_strategy(algorithm, self._mem_storage)
        return self._strategies[algorithm]

    async def increment(
        self,
        key: str,
        limit: str,
        algorithm: RateLimitAlgorithm,
    ) -> RateLimitResult:
        """Increment the counter and return a ``RateLimitResult``."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(algorithm)

        async with self._lock:
            allowed: bool = strategy.hit(item, key)
            stats = strategy.get_window_stats(item, key)

        reset_at = datetime.fromtimestamp(float(stats.reset_time), tz=UTC)
        return _build_result(
            key=key,
            limit_str=limit,
            allowed=allowed,
            remaining=int(stats.remaining),
            reset_at=reset_at,
        )

    async def get_remaining(self, key: str, limit: str) -> int:
        """Return remaining requests in the current window."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(self._default_algorithm)
        async with self._lock:
            stats = strategy.get_window_stats(item, key)
        return max(0, int(stats.remaining))

    async def reset(self, key: str) -> None:
        """Clear counters for the specific *key* from the internal storage."""
        async with self._lock:
            storage_dict: dict[str, Any] = getattr(self._mem_storage, "storage", {})
            to_delete = [k for k in storage_dict if key in k]
            for k in to_delete:
                storage_dict.pop(k, None)
            # Moving window uses a separate events dict.
            events_dict: dict[str, Any] = getattr(self._mem_storage, "events", {})
            for k in list(events_dict.keys()):
                if key in k:
                    events_dict.pop(k, None)

    async def reset_all_for_path(self, path: str) -> None:
        """Clear all counters whose keys contain *path*."""
        async with self._lock:
            storage_dict: dict[str, Any] = getattr(self._mem_storage, "storage", {})
            to_delete = [k for k in storage_dict if path in k]
            for k in to_delete:
                storage_dict.pop(k, None)
            events_dict: dict[str, Any] = getattr(self._mem_storage, "events", {})
            for k in list(events_dict.keys()):
                if path in k:
                    events_dict.pop(k, None)


class RedisRateLimitStorage(RateLimitStorage):
    """Redis-backed rate limit storage wrapping ``limits.RedisStorage``.

    Uses the same Redis connection URL as the existing ``RedisBackend``,
    so no duplicate configuration is required when the main backend is
    Redis.

    Supports multi-instance deployments — counters are atomic and shared
    across all workers via Redis.

    Parameters
    ----------
    redis_url:
        Redis connection URL, e.g. ``"redis://localhost:6379/0"``.
    default_algorithm:
        Default algorithm for ``get_remaining()``.
    """

    def __init__(
        self,
        redis_url: str,
        default_algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW,
    ) -> None:
        if not HAS_LIMITS:
            raise ImportError(
                "Rate limiting requires the 'limits' library. "
                "Install it with: pip install api-shield[rate-limit]"
            )
        from limits.storage import RedisStorage

        self._redis_storage = RedisStorage(redis_url)
        self._default_algorithm = default_algorithm
        self._strategies: dict[RateLimitAlgorithm, Any] = {}

    def _get_strategy(self, algorithm: RateLimitAlgorithm) -> Any:
        if algorithm not in self._strategies:
            self._strategies[algorithm] = _get_strategy(algorithm, self._redis_storage)
        return self._strategies[algorithm]

    async def increment(
        self,
        key: str,
        limit: str,
        algorithm: RateLimitAlgorithm,
    ) -> RateLimitResult:
        """Increment and check the rate limit counter in Redis."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(algorithm)
        allowed: bool = strategy.hit(item, key)
        stats = strategy.get_window_stats(item, key)
        reset_at = datetime.fromtimestamp(float(stats.reset_time), tz=UTC)
        return _build_result(
            key=key,
            limit_str=limit,
            allowed=allowed,
            remaining=int(stats.remaining),
            reset_at=reset_at,
        )

    async def get_remaining(self, key: str, limit: str) -> int:
        """Return remaining requests in the current window."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(self._default_algorithm)
        stats = strategy.get_window_stats(item, key)
        return max(0, int(stats.remaining))

    async def reset(self, key: str) -> None:
        """Clear all Redis keys matching ``*{key}*`` using ``SCAN``.

        The underlying ``limits`` Redis client is synchronous.  The blocking
        SCAN + DELETE loop is offloaded to a thread via ``asyncio.to_thread``
        so the event loop is not stalled during admin resets.
        """
        import redis as redis_lib

        # Use limits' underlying redis client if accessible.
        redis_client = getattr(self._redis_storage, "_storage", None) or getattr(
            self._redis_storage, "storage", None
        )
        if redis_client is None:
            logger.warning("shield: RedisRateLimitStorage.reset: cannot access redis client")
            return

        pattern = f"*{key}*"

        def _do_reset() -> None:
            cursor = 0
            while True:
                cursor, found_keys = redis_client.scan(cursor, match=pattern, count=100)
                if found_keys:
                    redis_client.delete(*found_keys)
                if cursor == 0:
                    break

        try:
            await asyncio.to_thread(_do_reset)
        except redis_lib.RedisError as exc:
            logger.warning("shield: rate limit reset failed: %s", exc)

    async def reset_all_for_path(self, path: str) -> None:
        """Clear all Redis keys matching ``*{_RATE_LIMIT_NAMESPACE}*{path}*``."""
        await self.reset(f"{_RATE_LIMIT_NAMESPACE}:{path}")


class FileRateLimitStorage(RateLimitStorage):
    """In-memory counters with periodic snapshot to the shield state file.

    Supports the same file formats as ``FileBackend``: JSON (``.json``),
    YAML (``.yaml`` / ``.yml``), and TOML (``.toml``).  The snapshot is
    always written back in the same format the file was read from.

    Counters survive process restarts for windows that are still active —
    the snapshot is restored with the remaining TTL so the window
    continues uninterrupted.  Expired windows reset naturally (no stale
    counters leaked into the next window).

    **Single-worker limitation**: each worker has its own counter because
    the in-memory counters are per-process.  ``ShieldProductionWarning``
    is emitted on instantiation.  For multi-worker deployments use
    ``RedisRateLimitStorage``.

    Parameters
    ----------
    file_path:
        Path to the shield state file (same file used by ``FileBackend``).
    snapshot_interval_seconds:
        How often to flush counters to disk in the background.  Default 10 s.
        Lower values mean less data lost on unclean shutdown but more disk I/O.
    """

    _WARNING_MESSAGE = (
        "FileBackend rate limiting uses in-memory counters with file-based "
        "persistence. Counters survive restarts for active windows. This is "
        "safe for single-process deployments only. Under multiple workers, "
        "each worker enforces the limit independently. For multi-worker "
        "deployments, use RedisBackend."
    )

    def __init__(
        self,
        file_path: str,
        snapshot_interval_seconds: int = 10,
    ) -> None:
        from shield.core.exceptions import ShieldProductionWarning

        if not HAS_LIMITS:
            raise ImportError(
                "Rate limiting requires the 'limits' library. "
                "Install it with: pip install api-shield[rate-limit]"
            )
        warnings.warn(self._WARNING_MESSAGE, ShieldProductionWarning, stacklevel=2)
        from limits.storage import MemoryStorage

        self._file_path = Path(file_path)
        self._snapshot_interval = snapshot_interval_seconds
        self._mem_storage = MemoryStorage()
        self._lock = asyncio.Lock()
        self._default_algorithm = RateLimitAlgorithm.FIXED_WINDOW
        self._strategies: dict[RateLimitAlgorithm, Any] = {}
        self._snapshot_task: asyncio.Task[None] | None = None
        # In-memory mirror of counters for snapshot serialisation.
        # Format: {key: {"count": int, "window_start": str,
        #                "window_duration_seconds": int, "limit": str}}
        self._counter_meta: dict[str, dict[str, Any]] = {}
        # File format derived from the extension — matches FileBackend behaviour.
        _ext = self._file_path.suffix.lower()
        self._file_format: str = {".yaml": "yaml", ".yml": "yaml", ".toml": "toml"}.get(
            _ext, "json"
        )

    def _parse_file(self, raw: str) -> dict[str, Any]:
        """Deserialise *raw* text using the file's detected format."""
        if self._file_format == "yaml":
            import yaml  # type: ignore[import-untyped]

            return dict(yaml.safe_load(raw) or {})
        if self._file_format == "toml":
            import tomllib

            return dict(tomllib.loads(raw))
        return dict(json.loads(raw))

    def _serialize_file(self, data: dict[str, Any]) -> str:
        """Serialise *data* to text using the file's detected format."""
        if self._file_format == "yaml":
            import yaml

            return str(yaml.dump(data, default_flow_style=False, allow_unicode=True))
        if self._file_format == "toml":
            import tomli_w

            return tomli_w.dumps(data)
        return json.dumps(data, default=str)

    def _get_strategy(self, algorithm: RateLimitAlgorithm) -> Any:
        if algorithm not in self._strategies:
            self._strategies[algorithm] = _get_strategy(algorithm, self._mem_storage)
        return self._strategies[algorithm]

    async def startup(self) -> None:
        """Start the background snapshot task.

        Called once by ``ShieldEngine`` after the storage is wired up,
        so the task is already running before the first request arrives.
        ``increment()`` still calls ``_ensure_snapshot_task()`` as a
        fallback for direct usage outside the engine.
        """
        self._ensure_snapshot_task()

    def _ensure_snapshot_task(self) -> None:
        """Start the background snapshot task if not already running."""
        if self._snapshot_task is None or self._snapshot_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._snapshot_task = loop.create_task(
                    self._background_snapshot(),
                    name="shield-rate-limit-snapshot",
                )
            except RuntimeError:
                pass  # no event loop — snapshot on demand

    async def _background_snapshot(self) -> None:
        """Periodically flush counters to disk."""
        try:
            while True:
                await asyncio.sleep(self._snapshot_interval)
                await self.flush_snapshot()
        except asyncio.CancelledError:
            pass

    async def flush_snapshot(self) -> None:
        """Write the current counter state to the shield state file."""
        async with self._lock:
            snapshot = dict(self._counter_meta)

        await self._write_snapshot(snapshot)

    async def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Merge the rate_limits section into the existing state file.

        The file is read, updated, and written back in its original format
        (JSON, YAML, or TOML) so the file is never corrupted by a format mismatch.
        """
        import aiofiles  # type: ignore[import-untyped]

        if not self._file_path.exists():
            return
        try:
            async with aiofiles.open(self._file_path) as f:
                raw = await f.read()
            data: dict[str, Any] = self._parse_file(raw) if raw.strip() else {}
            data["rate_limits"] = snapshot
            async with aiofiles.open(self._file_path, "w") as f:
                await f.write(self._serialize_file(data))
        except Exception as exc:
            logger.warning("shield: rate limit snapshot write failed: %s", exc)

    async def _restore_from_snapshot(self) -> None:
        """Restore counters from the state file on startup.

        Only counters whose windows have not yet expired are restored.
        Expired windows reset naturally — no stale counters leaked.
        """
        if not self._file_path.exists():
            return
        try:
            import aiofiles

            async with aiofiles.open(self._file_path) as f:
                raw = await f.read()
            data: dict[str, Any] = self._parse_file(raw) if raw.strip() else {}
            snapshot: dict[str, dict[str, Any]] = data.get("rate_limits", {})
        except Exception as exc:
            logger.warning("shield: rate limit snapshot restore failed: %s", exc)
            return

        now = datetime.now(UTC)
        for key, entry in snapshot.items():
            try:
                window_start = datetime.fromisoformat(entry["window_start"])
                window_end = window_start + timedelta(seconds=entry["window_duration_seconds"])
                if now >= window_end:
                    continue  # window expired — counter resets naturally
                # Restore counter metadata so future snapshots include it.
                self._counter_meta[key] = entry
            except Exception as exc:
                logger.debug("shield: skipping corrupt snapshot entry %r: %s", key, exc)

    async def increment(
        self,
        key: str,
        limit: str,
        algorithm: RateLimitAlgorithm,
    ) -> RateLimitResult:
        """Increment and check the rate limit counter."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(algorithm)

        async with self._lock:
            allowed: bool = strategy.hit(item, key)
            stats = strategy.get_window_stats(item, key)
            reset_at = datetime.fromtimestamp(float(stats.reset_time), tz=UTC)
            # Keep counter metadata for snapshot serialisation.
            count = item.amount - max(0, int(stats.remaining))
            window_duration = item.get_expiry()
            now = datetime.now(UTC)
            remaining_secs = (reset_at - now).total_seconds()
            window_start = now - timedelta(seconds=window_duration - remaining_secs)
            self._counter_meta[key] = {
                "count": count,
                "window_start": window_start.isoformat(),
                "window_duration_seconds": window_duration,
                "limit": limit,
            }

        # When used through ShieldEngine, startup() already started the task
        # so this is only a fast None-check on the hot path.  When used
        # directly (e.g. tests), this starts the task on first increment.
        if self._snapshot_task is None:
            self._ensure_snapshot_task()
        return _build_result(
            key=key,
            limit_str=limit,
            allowed=allowed,
            remaining=int(stats.remaining),
            reset_at=reset_at,
        )

    async def get_remaining(self, key: str, limit: str) -> int:
        """Return remaining requests in the current window."""
        from limits import parse

        item = parse(limit)
        strategy = self._get_strategy(self._default_algorithm)
        async with self._lock:
            stats = strategy.get_window_stats(item, key)
        return max(0, int(stats.remaining))

    async def reset(self, key: str) -> None:
        """Clear counters for the specific *key*."""
        async with self._lock:
            storage_dict: dict[str, Any] = getattr(self._mem_storage, "storage", {})
            to_delete = [k for k in storage_dict if key in k]
            for k in to_delete:
                storage_dict.pop(k, None)
            events_dict: dict[str, Any] = getattr(self._mem_storage, "events", {})
            for k in list(events_dict.keys()):
                if key in k:
                    events_dict.pop(k, None)
            # Remove from counter metadata.
            self._counter_meta.pop(key, None)

    async def reset_all_for_path(self, path: str) -> None:
        """Clear all counters whose keys contain *path*."""
        async with self._lock:
            storage_dict: dict[str, Any] = getattr(self._mem_storage, "storage", {})
            to_delete = [k for k in storage_dict if path in k]
            for k in to_delete:
                storage_dict.pop(k, None)
            events_dict: dict[str, Any] = getattr(self._mem_storage, "events", {})
            for k in list(events_dict.keys()):
                if path in k:
                    events_dict.pop(k, None)
            # Remove from counter metadata.
            for k in list(self._counter_meta.keys()):
                if path in k:
                    self._counter_meta.pop(k, None)

    async def shutdown(self) -> None:
        """Cancel the background snapshot task and write a final snapshot."""
        if self._snapshot_task is not None and not self._snapshot_task.done():
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass
            self._snapshot_task = None
        await self.flush_snapshot()


def create_rate_limit_storage(
    backend: ShieldBackend,
    rate_limit_backend: ShieldBackend | None = None,
    snapshot_interval_seconds: int = 10,
) -> RateLimitStorage:
    """Factory: create the appropriate ``RateLimitStorage`` for the given backends.

    Resolution order:
    1. If *rate_limit_backend* is provided, use it (regardless of main backend type).
    2. If main backend is ``RedisBackend``, create ``RedisRateLimitStorage`` (same URL).
    3. If main backend is ``MemoryBackend``, create ``MemoryRateLimitStorage``.
    4. If main backend is ``FileBackend``, create ``FileRateLimitStorage``
       (same file path, emits ``ShieldProductionWarning``).

    Parameters
    ----------
    backend:
        The main ``ShieldBackend`` used for route state.
    rate_limit_backend:
        Optional dedicated backend for rate limit counters.  When provided,
        routing decisions use this instead of the main backend's storage type.
    snapshot_interval_seconds:
        Passed to ``FileRateLimitStorage`` when the file backend is selected.
    """
    from shield.core.backends.file import FileBackend
    from shield.core.backends.memory import MemoryBackend

    # Prefer the explicitly-provided rate limit backend.
    effective = rate_limit_backend if rate_limit_backend is not None else backend

    try:
        from shield.core.backends.redis import RedisBackend

        if isinstance(effective, RedisBackend):
            # Extract the Redis URL from the connection pool.
            pool = getattr(effective, "_pool", None)
            url: str = (
                pool.connection_kwargs.get("host", "redis://localhost")
                if pool
                else "redis://localhost"
            )
            # Prefer the full URL from pool kwargs if available.
            connection_kwargs = pool.connection_kwargs if pool else {}
            host = connection_kwargs.get("host", "localhost")
            port = connection_kwargs.get("port", 6379)
            db = connection_kwargs.get("db", 0)
            password = connection_kwargs.get("password")
            if password:
                url = f"redis://:{password}@{host}:{port}/{db}"
            else:
                url = f"redis://{host}:{port}/{db}"
            return RedisRateLimitStorage(redis_url=url)
    except ImportError:
        pass

    if isinstance(effective, MemoryBackend):
        return MemoryRateLimitStorage()

    if isinstance(effective, FileBackend):
        file_path = str(getattr(effective, "_path", "shield_state.json"))
        return FileRateLimitStorage(
            file_path=file_path,
            snapshot_interval_seconds=snapshot_interval_seconds,
        )

    # Fallback — unknown backend type, use in-memory storage.
    logger.warning(
        "shield: unknown backend type %r — using MemoryRateLimitStorage for rate limits",
        type(effective).__name__,
    )
    return MemoryRateLimitStorage()
