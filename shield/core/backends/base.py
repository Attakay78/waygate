"""Abstract base class defining the backend contract for api-shield."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from shield.core.models import (
    AuditEntry,
    GlobalMaintenanceConfig,
    RouteState,
    RouteStatus,
)

# Reserved backend key used to persist global maintenance configuration.
# Hidden from user-facing ``list_states()`` results by the engine layer.
_GLOBAL_KEY = "__shield:global__"


class ShieldBackend(ABC):
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
    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
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
        """Called by ``ShieldEngine`` on startup.

        Override to open database connections, create tables, or perform
        any other async initialisation your backend requires.  The default
        implementation is a no-op, so built-in backends (MemoryBackend,
        FileBackend, RedisBackend) require no changes.
        """

    async def shutdown(self) -> None:
        """Called by ``ShieldEngine`` on shutdown.

        Override to close database connections or release resources.  The
        default implementation is a no-op.
        """

    async def subscribe(self) -> AsyncIterator[RouteState]:  # type: ignore[return]
        """Stream live ``RouteState`` changes as they occur.

        Backends that support pub/sub (e.g. Redis) should override this.
        Backends that do not support it raise ``NotImplementedError``,
        and the dashboard will fall back to polling ``list_states()``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support pub/sub subscriptions. "
            "Use polling instead."
        )
        # Make the type checker happy — this is never reached.
        yield  # type: ignore[misc]
