"""File-based backend for api-shield.

Supports JSON, YAML, and TOML file formats — auto-detected from the file
extension.  Each format requires its own optional dependency:

- ``.json``            — no extra dependency (stdlib ``json``)
- ``.yaml`` / ``.yml`` — requires ``pyyaml``  (``pip install pyyaml``)
- ``.toml``            — requires ``tomli-w``  (``pip install tomli-w``)
                         Reading uses Python 3.11+ stdlib ``tomllib``.

Format is chosen at construction time from the file extension.  The data
structure is identical across all formats; only the serialisation differs.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import aiofiles  # type: ignore[import-untyped]

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

_MAX_AUDIT_ENTRIES = 1000

# Supported extensions mapped to a canonical format name.
_EXT_TO_FORMAT: dict[str, str] = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


class FileBackend(ShieldBackend):
    """Backend that persists state to a file via ``aiofiles``.

    Survives process restarts.  Suitable for simple single-instance
    deployments.  A single ``asyncio.Lock`` prevents concurrent write
    corruption.

    The file format is auto-detected from the extension:

    =================== =================== ============================
    Extension           Format              Extra dependency
    =================== =================== ============================
    ``.json``           JSON                *(none — stdlib)*
    ``.yaml`` / ``.yml`` YAML              ``pip install pyyaml``
    ``.toml``           TOML               ``pip install tomli-w``
    =================== =================== ============================

    Data structure (shown as JSON — equivalent across all formats)::

        {
            "states": { "GET:/payments": { ...RouteState... }, ... },
            "audit":  [ { ...AuditEntry... }, ... ]
        }

    ``subscribe()`` raises ``NotImplementedError`` — use polling.

    Parameters
    ----------
    path:
        Path to the state file.  Created automatically if absent.
        The extension determines the serialisation format.

    Raises
    ------
    ValueError
        If the file extension is not one of ``.json``, ``.yaml``,
        ``.yml``, or ``.toml``.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

        ext = self._path.suffix.lower()
        if ext not in _EXT_TO_FORMAT:
            supported = ", ".join(sorted(_EXT_TO_FORMAT))
            raise ValueError(
                f"Unsupported file extension {ext!r} for FileBackend. "
                f"Supported extensions: {supported}"
            )
        self._format: str = _EXT_TO_FORMAT[ext]

    # ------------------------------------------------------------------
    # Format-specific serialisation
    # ------------------------------------------------------------------

    def _parse(self, raw: str) -> dict[str, Any]:
        """Deserialise *raw* text using the file's format."""
        if self._format == "json":
            return cast(dict[str, Any], json.loads(raw))

        if self._format == "yaml":
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ImportError(
                    "pyyaml is required for YAML FileBackend support. "
                    "Install it with: pip install pyyaml"
                ) from exc
            return cast(dict[str, Any], yaml.safe_load(raw) or {})

        # toml
        return tomllib.loads(raw)

    def _serialize(self, data: dict[str, Any]) -> str:
        """Serialise *data* to text using the file's format."""
        if self._format == "json":
            return json.dumps(data, default=str)

        if self._format == "yaml":
            try:
                import yaml
            except ImportError as exc:
                raise ImportError(
                    "pyyaml is required for YAML FileBackend support. "
                    "Install it with: pip install pyyaml"
                ) from exc
            return cast(str, yaml.dump(data, default_flow_style=False, allow_unicode=True))

        # toml
        try:
            import tomli_w
        except ImportError as exc:
            raise ImportError(
                "tomli-w is required to write TOML FileBackend files. "
                "Install it with: pip install tomli-w"
            ) from exc
        return tomli_w.dumps(data)

    # ------------------------------------------------------------------
    # Internal read / write
    # ------------------------------------------------------------------

    async def _read(self) -> dict[str, Any]:
        """Read and parse the state file.

        Returns an empty ``{"states": {}, "audit": []}`` structure if the
        file does not exist or is blank.
        """
        if not self._path.exists():
            return {"states": {}, "audit": []}
        async with aiofiles.open(self._path) as f:
            raw = await f.read()
        if not raw.strip():
            return {"states": {}, "audit": []}
        data = self._parse(raw)
        data.setdefault("states", {})
        data.setdefault("audit", [])
        return data

    async def _write(self, data: dict[str, Any]) -> None:
        """Serialise and write *data*, creating parent directories as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self._path, "w") as f:
            await f.write(self._serialize(data))

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

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*."""
        data = await self._read()
        entries = data["audit"]
        if path is not None:
            entries = [e for e in entries if e["path"] == path]
        return [AuditEntry.model_validate(e) for e in reversed(entries)][:limit]

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Not supported — raises ``NotImplementedError``."""
        raise NotImplementedError("FileBackend does not support pub/sub. Use polling instead.")
        yield
