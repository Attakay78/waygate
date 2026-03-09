"""File-based JSON backend for api-shield."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiofiles

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

_MAX_AUDIT_ENTRIES = 1000


class FileBackend(ShieldBackend):
    """Backend that persists state to a JSON file via ``aiofiles``.

    Survives process restarts. Suitable for simple single-instance deployments.
    A single ``asyncio.Lock`` prevents concurrent write corruption.

    File format::

        {
            "states": { "/path": { ...RouteState... } },
            "audit":  [ { ...AuditEntry... }, ... ]
        }

    ``subscribe()`` raises ``NotImplementedError`` — use polling.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read(self) -> dict[str, Any]:
        """Read and parse the JSON file.

        Returns an empty structure if the file does not exist.
        """
        if not self._path.exists():
            return {"states": {}, "audit": []}
        async with aiofiles.open(self._path) as f:
            raw = await f.read()
        return json.loads(raw) if raw.strip() else {"states": {}, "audit": []}

    async def _write(self, data: dict[str, Any]) -> None:
        """Write *data* to the JSON file atomically under the lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self._path, "w") as f:
            await f.write(json.dumps(data, default=str))

    # ------------------------------------------------------------------
    # ShieldBackend interface
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path*.

        Raises ``KeyError`` if no state has been registered for *path*.
        """
        data = await self._read()
        if path not in data["states"]:
            raise KeyError(f"No state registered for path {path!r}")
        return RouteState.model_validate(data["states"][path])

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path*."""
        async with self._lock:
            data = await self._read()
            data["states"][path] = json.loads(state.model_dump_json())
            await self._write(data)

    async def delete_state(self, path: str) -> None:
        """Remove state for *path*. No-op if not registered."""
        async with self._lock:
            data = await self._read()
            data["states"].pop(path, None)
            await self._write(data)

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        data = await self._read()
        return [RouteState.model_validate(v) for v in data["states"].values()]

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log, capping at 1000 entries."""
        async with self._lock:
            data = await self._read()
            data["audit"].append(json.loads(entry.model_dump_json()))
            if len(data["audit"]) > _MAX_AUDIT_ENTRIES:
                data["audit"] = data["audit"][-_MAX_AUDIT_ENTRIES:]
            await self._write(data)

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*."""
        data = await self._read()
        entries = data["audit"]
        if path is not None:
            entries = [e for e in entries if e["path"] == path]
        return [AuditEntry.model_validate(e) for e in reversed(entries)][:limit]

    async def subscribe(self) -> AsyncIterator[RouteState]:  # type: ignore[override]
        """Not supported — raises ``NotImplementedError``."""
        raise NotImplementedError(
            "FileBackend does not support pub/sub. Use polling instead."
        )
        yield  # type: ignore[misc]
