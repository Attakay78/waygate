"""In-process memory backend for api-shield."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

_MAX_AUDIT_ENTRIES = 1000


class MemoryBackend(ShieldBackend):
    """Backend that stores all state in-process.

    Default backend. Ideal for single-instance apps and testing.
    State is lost when the process restarts.
    """

    def __init__(self) -> None:
        self._states: dict[str, RouteState] = {}
        self._audit: list[AuditEntry] = []
        self._subscribers: list[asyncio.Queue[RouteState]] = []

    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path*.

        Raises ``KeyError`` if no state has been registered for *path*.
        """
        try:
            return self._states[path]
        except KeyError:
            raise KeyError(f"No state registered for path {path!r}")

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path* and notify any subscribers."""
        self._states[path] = state
        for queue in self._subscribers:
            await queue.put(state)

    async def delete_state(self, path: str) -> None:
        """Remove state for *path*. No-op if not registered."""
        self._states.pop(path, None)

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        return list(self._states.values())

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log, capping at 1000 entries."""
        self._audit.append(entry)
        if len(self._audit) > _MAX_AUDIT_ENTRIES:
            self._audit = self._audit[-_MAX_AUDIT_ENTRIES:]

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*."""
        entries = self._audit if path is None else [e for e in self._audit if e.path == path]
        return list(reversed(entries))[:limit]

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Yield ``RouteState`` objects as they are updated."""
        queue: asyncio.Queue[RouteState] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                state = await queue.get()
                yield state
        finally:
            self._subscribers.remove(queue)
