"""Abstract base class defining the backend contract for waygate."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from waygate.core.models import (
    AuditEntry,
    GlobalMaintenanceConfig,
    RouteState,
    RouteStatus,
)

if TYPE_CHECKING:
    from waygate.core.rate_limit.models import RateLimitHit

# Reserved backend key used to persist global maintenance configuration.
# Hidden from user-facing ``list_states()`` results by the engine layer.
_GLOBAL_KEY = "__waygate:global__"


class WaygateBackend(ABC):
    """Contract that all storage backends must implement.

    Backends are responsible for persisting route state and audit logs.
    The ``subscribe()`` method is optional — backends that don't support
    pub/sub should raise ``NotImplementedError`` and the dashboard will
    fall back to polling.
    """

    @abstractmethod
    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path*.

        Raises ``KeyError`` if no state has been registered for *path*.
        """

    @abstractmethod
    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path*, overwriting any existing entry."""

    @abstractmethod
    async def delete_state(self, path: str) -> None:
        """Remove all state for *path*.

        No-op if *path* is not registered.
        """

    @abstractmethod
    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""

    @abstractmethod
    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log."""

    @abstractmethod
    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first.

        If *path* is given, return only entries for that route.
        *limit* caps the number of entries returned.
        """

    # ------------------------------------------------------------------
    # Global maintenance configuration — concrete default implementation
    #
    # Stored as a sentinel RouteState with path ``_GLOBAL_KEY``.
    # The ``GlobalMaintenanceConfig`` is JSON-serialised into the
    # ``reason`` field of that sentinel entry.  This lets every backend
    # support global maintenance with zero subclass changes — backends
    # that want a dedicated storage path can override these two methods.
    # ------------------------------------------------------------------

    async def get_global_config(self) -> GlobalMaintenanceConfig:
        """Return the current global maintenance configuration."""
        try:
            state = await self.get_state(_GLOBAL_KEY)
            return GlobalMaintenanceConfig.model_validate_json(state.reason)
        except (KeyError, Exception):
            return GlobalMaintenanceConfig()

    async def set_global_config(self, config: GlobalMaintenanceConfig) -> None:
        """Persist *config* as the global maintenance configuration."""
        sentinel = RouteState(
            path=_GLOBAL_KEY,
            status=RouteStatus.ACTIVE,
            reason=config.model_dump_json(),
        )
        await self.set_state(_GLOBAL_KEY, sentinel)

    # ------------------------------------------------------------------
    # Lifecycle hooks — override these in backends that need async setup
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Called by ``WaygateEngine`` on startup.

        Override to open database connections, create tables, or perform
        any other async initialisation your backend requires.  The default
        implementation is a no-op, so built-in backends (MemoryBackend,
        FileBackend, RedisBackend) require no changes.
        """

    async def shutdown(self) -> None:
        """Called by ``WaygateEngine`` on shutdown.

        Override to close database connections or release resources.  The
        default implementation is a no-op.
        """

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Stream live ``RouteState`` changes as they occur.

        Backends that support pub/sub (e.g. Redis) should override this.
        Backends that do not support it raise ``NotImplementedError``,
        and the dashboard will fall back to polling ``list_states()``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support pub/sub subscriptions. Use polling instead."
        )
        # Unreachable — makes this an async generator so the return type is valid.
        yield

    async def try_claim_webhook_dispatch(self, dedup_key: str, ttl_seconds: int = 60) -> bool:
        """Attempt to claim exclusive webhook dispatch rights for *dedup_key*.

        Called by ``WaygateEngine._fire_webhooks`` before dispatching to any
        registered webhook URLs.  Returns ``True`` if this instance should
        fire the webhooks, ``False`` if another instance has already claimed
        the right for the same event.

        The default implementation always returns ``True`` — single-instance
        backends (``MemoryBackend``, ``FileBackend``) never have concurrent
        instances so deduplication is unnecessary.

        ``RedisBackend`` overrides this with a ``SET NX`` command to ensure
        only one instance fires webhooks per unique event across an entire
        multi-instance deployment.

        Parameters
        ----------
        dedup_key:
            A deterministic string that uniquely identifies this event
            (derived from ``event + path + serialised RouteState``).
        ttl_seconds:
            How long the claim key lives in the backend.  After this window
            the key expires, allowing re-delivery if the claiming instance
            crashed before it could dispatch.  Defaults to 60 seconds.
        """
        return True

    async def subscribe_global_config(self) -> AsyncIterator[None]:
        """Stream a signal whenever the global maintenance config changes.

        Yields ``None`` on each remote change so callers can invalidate their
        in-process cache and re-fetch from the backend.

        Backends that support this (e.g. ``RedisBackend``) override this
        method.  Others raise ``NotImplementedError`` — ``WaygateEngine.start()``
        checks for this and simply skips starting the listener, so the engine
        falls back to the per-process cache behaviour without any error.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support global config pub/sub.")
        # Unreachable — makes this a valid async generator return type.
        yield

    async def get_registered_paths(self) -> set[str]:
        """Return the set of all registered path keys for deduplication.

        Used by ``WaygateEngine.register_batch()`` to detect already-registered
        routes without re-querying the full state list.  The default
        implementation derives the set from ``list_states()``; backends that
        store routes under transformed keys (e.g. ``WaygateServerBackend`` which
        adds a service prefix) should override this to return local/plain keys.
        """
        return {s.path for s in await self.list_states()}

    # ------------------------------------------------------------------
    # Rate limit hit log — concrete default implementations
    # ------------------------------------------------------------------

    async def write_rate_limit_hit(self, hit: RateLimitHit) -> None:
        """Append a rate limit hit record to the backend log.

        Default implementation is a no-op — backends that support persistent
        hit logs (``FileBackend``, ``RedisBackend``) override this.
        ``MemoryBackend`` provides an in-memory list implementation.
        """

    async def get_rate_limit_hits(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[RateLimitHit]:
        """Return recent rate limit hits, newest first.

        When *path* is given, return only hits for that route.
        Default implementation returns an empty list — override in backends
        that store hits persistently.
        """
        return []

    # ------------------------------------------------------------------
    # Rate limit policy persistence — concrete default implementations
    # ------------------------------------------------------------------

    async def set_rate_limit_policy(
        self, path: str, method: str, policy_data: dict[str, Any]
    ) -> None:
        """Persist a rate limit policy for *path*/*method*.

        *policy_data* is a JSON-serialisable dict matching the
        ``RateLimitPolicy`` schema.  Overwrites any existing policy for
        the same path/method pair.

        Default is a no-op.  ``MemoryBackend``, ``FileBackend``, and
        ``RedisBackend`` override this to provide real persistence.
        """

    async def get_rate_limit_policies(self) -> list[dict[str, Any]]:
        """Return all persisted rate limit policies.

        Each item is a JSON-serialisable dict matching the
        ``RateLimitPolicy`` schema.  Returns an empty list by default.
        """
        return []

    async def delete_rate_limit_policy(self, path: str, method: str) -> None:
        """Remove the persisted rate limit policy for *path*/*method*.

        No-op if no policy is stored for that pair.
        Default implementation is a no-op.
        """

    # ------------------------------------------------------------------
    # Global rate limit policy persistence — concrete default implementations
    #
    # Stored as a sentinel RouteState with path ``_GLOBAL_RL_KEY``.
    # The ``GlobalRateLimitPolicy`` is JSON-serialised into the ``reason``
    # field.  Same pattern as global maintenance config — no subclass changes
    # required for existing backends.
    # ------------------------------------------------------------------

    async def get_global_rate_limit_policy(self) -> dict[str, Any] | None:
        """Return the persisted global rate limit policy dict, or ``None``."""
        _GLOBAL_RL_KEY = "__waygate:global_rl__"
        try:
            state = await self.get_state(_GLOBAL_RL_KEY)
            import json

            return dict(json.loads(state.reason))
        except (KeyError, Exception):
            return None

    async def set_global_rate_limit_policy(self, policy_data: dict[str, Any]) -> None:
        """Persist *policy_data* as the global rate limit policy."""
        import json

        _GLOBAL_RL_KEY = "__waygate:global_rl__"
        from waygate.core.models import RouteStatus

        sentinel = RouteState(
            path=_GLOBAL_RL_KEY,
            status=RouteStatus.ACTIVE,
            reason=json.dumps(policy_data),
        )
        await self.set_state(_GLOBAL_RL_KEY, sentinel)

    async def delete_global_rate_limit_policy(self) -> None:
        """Remove the persisted global rate limit policy."""
        _GLOBAL_RL_KEY = "__waygate:global_rl__"
        await self.delete_state(_GLOBAL_RL_KEY)

    # ------------------------------------------------------------------
    # Per-service rate limit policy persistence — concrete default
    # implementations using the sentinel RouteState pattern.
    # Stored as a sentinel with path ``__waygate:svc_rl:{service}__``.
    # No subclass changes required for existing backends.
    # ------------------------------------------------------------------

    async def get_service_rate_limit_policy(self, service: str) -> dict[str, Any] | None:
        """Return the persisted per-service rate limit policy dict, or ``None``."""
        import json

        key = f"__waygate:svc_rl:{service}__"
        try:
            state = await self.get_state(key)
            return dict(json.loads(state.reason))
        except (KeyError, Exception):
            return None

    async def set_service_rate_limit_policy(
        self, service: str, policy_data: dict[str, Any]
    ) -> None:
        """Persist *policy_data* as the rate limit policy for *service*."""
        import json

        key = f"__waygate:svc_rl:{service}__"
        sentinel = RouteState(
            path=key,
            status=RouteStatus.ACTIVE,
            reason=json.dumps(policy_data),
            service=service,
        )
        await self.set_state(key, sentinel)

    async def delete_service_rate_limit_policy(self, service: str) -> None:
        """Remove the persisted rate limit policy for *service*."""
        key = f"__waygate:svc_rl:{service}__"
        await self.delete_state(key)

    async def get_all_service_rate_limit_policies(self) -> dict[str, dict[str, Any]]:
        """Return all persisted per-service rate limit policies as ``{service: policy_data}``."""
        import json

        result: dict[str, dict[str, Any]] = {}
        for state in await self.list_states():
            if state.path.startswith("__waygate:svc_rl:") and state.path.endswith("__"):
                svc = state.path[len("__waygate:svc_rl:") : -2]
                try:
                    result[svc] = dict(json.loads(state.reason))
                except Exception:
                    pass
        return result

    async def subscribe_rate_limit_policy(self) -> AsyncIterator[dict[str, Any]]:
        """Stream rate limit policy changes as they occur.

        Each yielded dict has the shape::

            {"action": "set",    "key": "GET:/api/orders", "policy": {...}}
            {"action": "delete", "key": "GET:/api/orders"}

        Backends that support pub/sub (e.g. ``RedisBackend``) override this.
        Others raise ``NotImplementedError`` — ``WaygateEngine.start()`` treats
        that as "single-instance mode" and skips the listener task.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support rate limit policy pub/sub."
        )
        yield  # make this a valid async generator

    # ------------------------------------------------------------------
    # Feature flag storage — concrete in-memory default implementations
    #
    # All backends get basic in-memory flag/segment storage for free.
    # FileBackend and RedisBackend can override for persistence.
    # Storage is lazily initialised on first use so existing backends
    # that do not call super().__init__() are not affected.
    # ------------------------------------------------------------------

    def _flag_store(self) -> dict[str, Any]:
        """Lazy per-instance dict for flag objects."""
        if not hasattr(self, "_flag_store_dict"):
            object.__setattr__(self, "_flag_store_dict", {})
        return self._flag_store_dict  # type: ignore[attr-defined, no-any-return]

    def _segment_store(self) -> dict[str, Any]:
        """Lazy per-instance dict for segment objects."""
        if not hasattr(self, "_segment_store_dict"):
            object.__setattr__(self, "_segment_store_dict", {})
        return self._segment_store_dict  # type: ignore[attr-defined, no-any-return]

    async def load_all_flags(self) -> list[Any]:
        """Return all stored feature flags.

        Returns a list of ``FeatureFlag`` objects.  The default
        implementation uses an in-memory store.  Override for persistent
        backends.
        """
        return list(self._flag_store().values())

    async def save_flag(self, flag: Any) -> None:
        """Persist *flag* (a ``FeatureFlag`` instance) by its key.

        Default implementation keeps flags in memory.  Override for
        persistent backends.
        """
        self._flag_store()[flag.key] = flag

    async def delete_flag(self, flag_key: str) -> None:
        """Remove the flag with *flag_key* from storage.

        No-op if the flag does not exist.
        """
        self._flag_store().pop(flag_key, None)

    async def load_all_segments(self) -> list[Any]:
        """Return all stored segments.

        Returns a list of ``Segment`` objects.  The default
        implementation uses an in-memory store.  Override for persistent
        backends.
        """
        return list(self._segment_store().values())

    async def save_segment(self, segment: Any) -> None:
        """Persist *segment* (a ``Segment`` instance) by its key.

        Default implementation keeps segments in memory.  Override for
        persistent backends.
        """
        self._segment_store()[segment.key] = segment

    async def delete_segment(self, segment_key: str) -> None:
        """Remove the segment with *segment_key* from storage.

        No-op if the segment does not exist.
        """
        self._segment_store().pop(segment_key, None)
