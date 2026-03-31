"""In-process memory backend for waygate."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from waygate.core.backends.base import WaygateBackend
from waygate.core.models import AuditEntry, RouteState

if TYPE_CHECKING:
    from waygate.core.rate_limit.models import RateLimitHit

_MAX_AUDIT_ENTRIES = 1000
_DEFAULT_MAX_RL_HIT_ENTRIES = 10_000


class MemoryBackend(WaygateBackend):
    """Backend that stores all state in-process.

    Default backend. Ideal for single-instance apps and testing.
    State is lost when the process restarts.

    Audit log is stored in a ``deque`` (O(1) append/evict) with a parallel
    per-path index (``dict[path, list[AuditEntry]]``) so that filtered
    queries — ``get_audit_log(path=...)`` — are O(k) where k is the number
    of entries for that specific path, not O(total entries).

    Rate limit hits are **aggregated**: consecutive blocks from the same
    ``(path, method, key)`` are grouped into a single ``RateLimitHit``
    entry whose ``count`` increments and ``last_hit_at`` advances on every
    subsequent block.  A new group is started once ``max_rl_hits_per_group``
    is reached.  This prevents a flood of 429s from a single client from
    saturating the hit log.
    """

    def __init__(
        self,
        max_rl_hit_entries: int = _DEFAULT_MAX_RL_HIT_ENTRIES,
    ) -> None:
        self._states: dict[str, RouteState] = {}
        # Ordered audit log — deque gives O(1) append and O(1) popleft eviction.
        self._audit: deque[AuditEntry] = deque()
        # Per-path index for O(1)-lookup filtered audit queries.
        self._audit_by_path: defaultdict[str, list[AuditEntry]] = defaultdict(list)
        self._subscribers: list[asyncio.Queue[RouteState]] = []
        self._max_rl_hit_entries = max_rl_hit_entries
        # Rate limit hit log — newest-first deque, capped at max_rl_hit_entries.
        self._rate_limit_hits: deque[RateLimitHit] = deque()
        self._rl_hits_by_path: defaultdict[str, list[RateLimitHit]] = defaultdict(list)
        # Rate limit policy store — keyed "METHOD:/path" → policy dict.
        self._rl_policies: dict[str, dict[str, Any]] = {}
        # Subscribers for rate limit policy changes.
        self._rl_policy_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

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
            queue.put_nowait(state)

    async def delete_state(self, path: str) -> None:
        """Remove state for *path*. No-op if not registered."""
        self._states.pop(path, None)

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        return list(self._states.values())

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log, capping at 1000 entries.

        When the cap is reached the oldest entry is evicted from both the
        ordered deque and the per-path index in O(1) / O(k) time respectively,
        where k is the number of entries for the evicted path (≪ total entries).
        """
        if len(self._audit) >= _MAX_AUDIT_ENTRIES:
            evicted = self._audit.popleft()
            # Clean up the per-path index for the evicted entry.
            path_list = self._audit_by_path.get(evicted.path)
            if path_list:
                try:
                    path_list.remove(evicted)
                except ValueError:
                    pass

        self._audit.append(entry)
        self._audit_by_path[entry.path].append(entry)

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*.

        When *path* is provided the per-path index is used — O(k) where k is
        the number of entries for that route — instead of scanning all 1000
        entries (O(N)).
        """
        if path is None:
            return list(reversed(self._audit))[:limit]
        path_entries = self._audit_by_path.get(path, [])
        return list(reversed(path_entries))[:limit]

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Yield ``RouteState`` objects as they are updated."""
        queue: asyncio.Queue[RouteState] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                state = await queue.get()
                yield state
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)

    async def write_rate_limit_hit(self, hit: RateLimitHit) -> None:
        """Append a rate limit hit record, evicting the oldest when the cap is reached."""
        if len(self._rate_limit_hits) >= self._max_rl_hit_entries:
            evicted = self._rate_limit_hits.popleft()
            path_list = self._rl_hits_by_path.get(evicted.path)
            if path_list:
                try:
                    path_list.remove(evicted)
                except ValueError:
                    pass

        self._rate_limit_hits.append(hit)
        self._rl_hits_by_path[hit.path].append(hit)

    async def get_rate_limit_hits(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[RateLimitHit]:
        """Return rate limit hits, newest first, optionally filtered by *path*."""
        if path is None:
            return list(reversed(self._rate_limit_hits))[:limit]
        path_entries = self._rl_hits_by_path.get(path, [])
        return list(reversed(path_entries))[:limit]

    async def set_rate_limit_policy(
        self, path: str, method: str, policy_data: dict[str, Any]
    ) -> None:
        """Persist *policy_data* for *path*/*method* and notify subscribers."""
        key = f"{method.upper()}:{path}"
        self._rl_policies[key] = policy_data
        event: dict[str, Any] = {"action": "set", "key": key, "policy": policy_data}
        for q in self._rl_policy_subscribers:
            q.put_nowait(event)

    async def get_rate_limit_policies(self) -> list[dict[str, Any]]:
        """Return all persisted rate limit policies."""
        return list(self._rl_policies.values())

    async def delete_rate_limit_policy(self, path: str, method: str) -> None:
        """Remove the persisted rate limit policy for *path*/*method* and notify subscribers."""
        key = f"{method.upper()}:{path}"
        self._rl_policies.pop(key, None)
        event: dict[str, Any] = {"action": "delete", "key": key}
        for q in self._rl_policy_subscribers:
            q.put_nowait(event)

    async def subscribe_rate_limit_policy(self) -> AsyncIterator[dict[str, Any]]:
        """Yield rate limit policy change events as they occur."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._rl_policy_subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(ValueError):
                self._rl_policy_subscribers.remove(queue)
