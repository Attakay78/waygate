"""File-based backend for api-shield.

Supports JSON, YAML, and TOML file formats — auto-detected from the file
extension.  Each format requires its own optional dependency:

- ``.json``            — no extra dependency (stdlib ``json``)
- ``.yaml`` / ``.yml`` — requires ``pyyaml``  (``pip install pyyaml``)
- ``.toml``            — requires ``tomli-w``  (``pip install tomli-w``)
                         Reading uses Python 3.11+ stdlib ``tomllib``.

Format is chosen at construction time from the file extension.  The data
structure is identical across all formats; only the serialisation differs.

Performance design
------------------
The original implementation read and wrote the entire file on every
``get_state`` / ``set_state`` / ``write_audit`` call — O(N) file I/O per
operation.

This version introduces a **write-through in-memory cache**:

* All reads are served from the in-memory ``_states`` dict — zero file I/O.
* Writes update the in-memory dict immediately (O(1)), then schedule a
  **debounced disk flush** (50 ms window).  Rapid sequential writes are
  coalesced into a single file write.
* A dedicated ``_io_lock`` serialises concurrent flushes so the file is
  never corrupted by interleaved writes.
* The cache is populated lazily on the first operation via ``_ensure_loaded``.
* ``shutdown()`` cancels any pending debounce and flushes synchronously.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiofiles  # type: ignore[import-untyped]

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

if TYPE_CHECKING:
    from shield.core.rate_limit.models import RateLimitHit

_MAX_AUDIT_ENTRIES = 1000

# Debounce window: rapid sequential writes are coalesced into one disk flush.
_WRITE_DEBOUNCE_SECONDS = 0.05

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
    deployments.

    All reads are served from an in-memory cache populated on first access.
    Writes update the cache immediately then schedule a debounced disk flush —
    meaning rapid bursts of state changes (e.g. startup route registration)
    result in a single file write rather than N writes.

    The file format is auto-detected from the extension:

    | Extension | Format | Extra dependency |
    |---|---|---|
    | `.json` | JSON | *(none — stdlib)* |
    | `.yaml` / `.yml` | YAML | `pip install pyyaml` |
    | `.toml` | TOML | `pip install tomli-w` |

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

    def __init__(
        self,
        path: str,
        max_rl_hit_entries: int = 10_000,
    ) -> None:
        self._path = Path(path)
        # Serializes concurrent disk flushes — held only during I/O, not
        # during in-memory mutations, so reads are never blocked.
        self._io_lock = asyncio.Lock()
        # Guards the initial file load to prevent duplicate reads when
        # multiple coroutines first access the backend concurrently.
        self._load_lock = asyncio.Lock()

        ext = self._path.suffix.lower()
        if ext not in _EXT_TO_FORMAT:
            supported = ", ".join(sorted(_EXT_TO_FORMAT))
            raise ValueError(
                f"Unsupported file extension {ext!r} for FileBackend. "
                f"Supported extensions: {supported}"
            )
        self._format: str = _EXT_TO_FORMAT[ext]

        self._max_rl_hit_entries = max_rl_hit_entries

        # In-memory cache — populated lazily by _ensure_loaded().
        # Raw dicts (not Pydantic models) are stored so that _flush_to_disk()
        # can snapshot and serialise without re-encoding.
        self._states: dict[str, Any] = {}
        self._audit: list[dict[str, Any]] = []
        self._loaded: bool = False
        # Rate limit hit log — capped at max_rl_hit_entries.
        self._rl_hits: list[dict[str, Any]] = []
        # Rate limit policy store — keyed "METHOD:/path" → policy dict.
        self._rl_policies: dict[str, Any] = {}

        # Debounced write task — cancelled and replaced on each write so that
        # a burst of N writes results in one disk flush rather than N.
        self._pending_write: asyncio.Task[None] | None = None

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

    async def _read_from_disk(self) -> dict[str, Any]:
        """Read and parse the state file from disk.

        Returns an empty ``{"states": {}, "audit": [], "rl_hits": []}``
        structure if the file does not exist or is blank.  Called only once
        during cache initialisation — all subsequent reads go through the
        in-memory cache.
        """
        if not self._path.exists():
            return {"states": {}, "audit": [], "rl_hits": [], "rl_policies": {}}
        async with aiofiles.open(self._path) as f:
            raw = await f.read()
        if not raw.strip():
            return {"states": {}, "audit": [], "rl_hits": [], "rl_policies": {}}
        data = self._parse(raw)
        data.setdefault("states", {})
        data.setdefault("audit", [])
        data.setdefault("rl_hits", [])
        data.setdefault("rl_policies", {})
        return data

    async def _write(self, data: dict[str, Any]) -> None:
        """Serialise and write *data*, creating parent directories as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self._path, "w") as f:
            await f.write(self._serialize(data))

    async def _ensure_loaded(self) -> None:
        """Populate the in-memory cache from disk on first access.

        Uses a lock + double-check to ensure exactly one disk read even when
        multiple coroutines call a backend method concurrently before the
        cache is warm.
        """
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:  # another coroutine beat us here
                return
            data = await self._read_from_disk()
            self._states = data["states"]
            self._audit = data["audit"]
            self._rl_hits = data.get("rl_hits", [])
            self._rl_policies = data.get("rl_policies", {})
            self._loaded = True

    def _schedule_write(self) -> None:
        """Schedule a debounced disk flush.

        Each call cancels any previously-scheduled flush and creates a new
        one, so a burst of N writes within the debounce window results in
        exactly one disk write.  Falls back to a no-op when there is no
        running event loop (e.g. during synchronous test teardown).
        """
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop — write will be flushed on shutdown()
        self._pending_write = loop.create_task(self._debounced_write())

    async def _debounced_write(self) -> None:
        """Wait the debounce window then flush to disk."""
        try:
            await asyncio.sleep(_WRITE_DEBOUNCE_SECONDS)
            await self._flush_to_disk()
        except asyncio.CancelledError:
            pass  # a newer write superseded us — that task will flush instead

    async def _flush_to_disk(self) -> None:
        """Snapshot the current in-memory state and write it to disk.

        The ``_io_lock`` prevents concurrent flushes from interleaving.
        The snapshot is taken inside the lock so the written data is always
        consistent — no partial view of an in-progress update.

        In asyncio, dict operations without ``await`` are atomic (cooperative
        scheduling guarantees no interleaving between two non-awaiting
        statements), so snapshotting inside the lock is sufficient.
        """
        async with self._io_lock:
            data: dict[str, Any] = {
                "states": dict(self._states),
                "audit": list(self._audit),
                "rl_hits": list(self._rl_hits),
                "rl_policies": dict(self._rl_policies),
            }
            await self._write(data)

    # ------------------------------------------------------------------
    # ShieldBackend interface
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the current state for *path* from the in-memory cache.

        Raises ``KeyError`` if no state has been registered for *path*.
        Zero file I/O after the cache is warm.
        """
        await self._ensure_loaded()
        raw = self._states.get(path)
        if raw is None:
            raise KeyError(f"No state registered for path {path!r}")
        return RouteState.model_validate(raw)

    async def set_state(self, path: str, state: RouteState) -> None:
        """Update the in-memory cache and flush to disk immediately.

        State changes are written synchronously so that a second
        ``FileBackend`` instance (e.g. the CLI) reading the same file sees
        the update right away.  Unlike ``write_audit``, state mutations
        are not debounced — durability is more important than batching here.
        """
        await self._ensure_loaded()
        self._states[path] = json.loads(state.model_dump_json())
        # Cancel any pending debounced audit flush — the full flush below
        # will include both the new state and any queued audit entries.
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
            self._pending_write = None
        await self._flush_to_disk()

    async def delete_state(self, path: str) -> None:
        """Remove state for *path* from cache and flush to disk immediately.

        No-op if *path* is not registered.
        """
        await self._ensure_loaded()
        self._states.pop(path, None)
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
            self._pending_write = None
        await self._flush_to_disk()

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states from the in-memory cache.

        Zero file I/O after the cache is warm.
        """
        await self._ensure_loaded()
        return [RouteState.model_validate(v) for v in self._states.values()]

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the in-memory audit log (capped at 1000 entries)
        and schedule a debounced disk flush.
        """
        await self._ensure_loaded()
        self._audit.append(json.loads(entry.model_dump_json()))
        if len(self._audit) > _MAX_AUDIT_ENTRIES:
            # Trim to the most-recent 1000 entries.
            del self._audit[: len(self._audit) - _MAX_AUDIT_ENTRIES]
        self._schedule_write()

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*.

        Served entirely from the in-memory cache — zero file I/O.
        """
        await self._ensure_loaded()
        entries = self._audit
        if path is not None:
            entries = [e for e in entries if e["path"] == path]
        return [AuditEntry.model_validate(e) for e in reversed(entries)][:limit]

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Not supported — raises ``NotImplementedError``."""
        raise NotImplementedError("FileBackend does not support pub/sub. Use polling instead.")
        yield

    async def write_rate_limit_hit(self, hit: RateLimitHit) -> None:
        """Append a rate limit hit record, evicting the oldest when the cap is reached."""
        await self._ensure_loaded()
        self._rl_hits.append(json.loads(hit.model_dump_json()))
        if len(self._rl_hits) > self._max_rl_hit_entries:
            del self._rl_hits[: len(self._rl_hits) - self._max_rl_hit_entries]
        self._schedule_write()

    async def get_rate_limit_hits(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[RateLimitHit]:
        """Return rate limit hits, newest first, optionally filtered by *path*."""
        from shield.core.rate_limit.models import RateLimitHit as RLHit

        await self._ensure_loaded()
        entries = self._rl_hits
        if path is not None:
            entries = [e for e in entries if e.get("path") == path]
        return [RLHit.model_validate(e) for e in reversed(entries)][:limit]

    async def set_rate_limit_policy(
        self, path: str, method: str, policy_data: dict[str, Any]
    ) -> None:
        """Persist *policy_data* for *path*/*method* and flush to disk."""
        await self._ensure_loaded()
        self._rl_policies[f"{method.upper()}:{path}"] = policy_data
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
            self._pending_write = None
        await self._flush_to_disk()

    async def get_rate_limit_policies(self) -> list[dict[str, Any]]:
        """Return all persisted rate limit policies."""
        await self._ensure_loaded()
        return list(self._rl_policies.values())

    async def delete_rate_limit_policy(self, path: str, method: str) -> None:
        """Remove the persisted rate limit policy for *path*/*method*."""
        await self._ensure_loaded()
        self._rl_policies.pop(f"{method.upper()}:{path}", None)
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
            self._pending_write = None
        await self._flush_to_disk()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Flush any pending write and release resources.

        Cancels the debounce timer and performs a synchronous flush so that
        in-flight state changes are not lost on graceful shutdown.
        """
        if self._pending_write is not None and not self._pending_write.done():
            self._pending_write.cancel()
            self._pending_write = None
        if self._loaded:
            await self._flush_to_disk()
