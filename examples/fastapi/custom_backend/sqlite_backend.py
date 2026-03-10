"""Custom Backend Example — SQLite via aiosqlite.

This file shows how to wire api-shield to a storage layer it does not ship
with by implementing the ``ShieldBackend`` abstract base class.

The contract is simple: implement six async methods and api-shield handles the
rest (engine logic, middleware, decorators, CLI, audit log).

Requirements:
    pip install aiosqlite
    # or: uv add aiosqlite

Run the demo app:
    uv run uvicorn examples.fastapi.custom_backend.sqlite_backend:app --reload

Use with the CLI:
    SHIELD_BACKEND=custom \\
    SHIELD_CUSTOM_PATH=examples.fastapi.custom_backend.sqlite_backend:make_backend \\
    SHIELD_SQLITE_PATH=shield-state.db \\
        shield status

    # or put this in your .shield file:
    #   SHIELD_BACKEND=custom
    #   SHIELD_CUSTOM_PATH=examples.fastapi.custom_backend.sqlite_backend:make_backend
    #   SHIELD_SQLITE_PATH=shield-state.db
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import FastAPI

from shield.core.backends.base import ShieldBackend
from shield.core.engine import ShieldEngine
from shield.core.models import AuditEntry, RouteState
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    disabled,
    force_active,
    maintenance,
)

# ---------------------------------------------------------------------------
# SQLiteBackend — implements the ShieldBackend contract
#
# Rules to follow when building any custom backend:
#
#   1. Subclass ``ShieldBackend`` from ``shield.core.backends.base``.
#   2. Implement all six @abstractmethod methods.
#   3. Override ``startup()`` / ``shutdown()`` for async initialisation —
#      the engine calls these automatically when used as ``async with engine:``.
#      The CLI also calls them via its ``async with _make_engine() as engine:``
#      pattern, so no special CLI wiring is needed.
#   4. RouteState and AuditEntry are Pydantic models — use .model_dump_json()
#      to serialise and .model_validate_json() to deserialise.
#   5. get_state() must raise KeyError when the path is not found.
#   6. Fail-open: let exceptions bubble up — ShieldEngine wraps every backend
#      call in try/except and allows the request through on failure.
#   7. subscribe() is optional. Leave it as-is if your backend doesn't support
#      pub/sub (the base class raises NotImplementedError and the dashboard
#      falls back to polling).
# ---------------------------------------------------------------------------

_MAX_AUDIT_ROWS = 1000

_CREATE_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS shield_states (
    path      TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
)
"""

_CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS shield_audit (
    id           TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    path         TEXT NOT NULL,
    entry_json   TEXT NOT NULL
)
"""


class SQLiteBackend(ShieldBackend):
    """api-shield backend backed by a SQLite database.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Use ``:memory:`` for an in-process
        database (useful for tests — not shared across processes).

    CLI usage
    ---------
    Point ``SHIELD_BACKEND`` to a zero-arg factory that returns a configured
    instance (see ``make_backend()`` below):

        SHIELD_BACKEND=myapp.backends:make_backend shield status

    The factory is called with no arguments, so it must read any configuration
    (like ``db_path``) from its own environment variables.

    Example
    -------
    >>> backend = SQLiteBackend("shield-state.db")
    >>> engine  = ShieldEngine(backend=backend)
    >>> async with engine:           # calls startup() then shutdown()
    ...     states = await engine.list_states()
    """

    def __init__(self, db_path: str | Path = "shield-state.db") -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle hooks — called automatically by ShieldEngine
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open the database connection and create tables if needed.

        Called automatically by ``ShieldEngine.__aenter__``.  You do not
        need to call this yourself when using ``async with engine:``.
        """
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = sqlite3.Row
        await self._db.execute(_CREATE_STATES_TABLE)
        await self._db.execute(_CREATE_AUDIT_TABLE)
        await self._db.commit()

    async def shutdown(self) -> None:
        """Close the database connection.

        Called automatically by ``ShieldEngine.__aexit__``.
        """
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "SQLiteBackend is not connected. "
                "Use 'async with engine:' to ensure startup() is called."
            )
        return self._db

    # ------------------------------------------------------------------
    # ShieldBackend — required methods
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the stored state for *path*.

        Raises ``KeyError`` if the path has not been registered yet —
        this is the contract the engine relies on to distinguish
        "not registered" from "registered but active".
        """
        async with self._conn.execute(
            "SELECT state_json FROM shield_states WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(f"No state registered for path {path!r}")

        return RouteState.model_validate_json(row["state_json"])

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path*, creating or replacing the existing row."""
        await self._conn.execute(
            """
            INSERT INTO shield_states (path, state_json)
            VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET state_json = excluded.state_json
            """,
            (path, state.model_dump_json()),
        )
        await self._conn.commit()

    async def delete_state(self, path: str) -> None:
        """Remove the state row for *path*. No-op if not found."""
        await self._conn.execute("DELETE FROM shield_states WHERE path = ?", (path,))
        await self._conn.commit()

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        async with self._conn.execute("SELECT state_json FROM shield_states") as cursor:
            rows = await cursor.fetchall()
        return [RouteState.model_validate_json(row["state_json"]) for row in rows]

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log, capping the table at 1000 rows."""
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO shield_audit (id, timestamp, path, entry_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.timestamp.isoformat(),
                entry.path,
                entry.model_dump_json(),
            ),
        )
        # Keep the table from growing unbounded.
        await self._conn.execute(
            """
            DELETE FROM shield_audit
            WHERE id NOT IN (
                SELECT id FROM shield_audit
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (_MAX_AUDIT_ROWS,),
        )
        await self._conn.commit()

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit entries, newest first, optionally filtered by *path*."""
        if path is not None:
            query = """
                SELECT entry_json FROM shield_audit
                WHERE path = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (path, limit)
        else:
            query = """
                SELECT entry_json FROM shield_audit
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params = (limit,)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return [AuditEntry.model_validate_json(row["entry_json"]) for row in rows]

    # ------------------------------------------------------------------
    # subscribe() — not implemented; dashboard falls back to polling
    # ------------------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[RouteState]:  # type: ignore[return]
        raise NotImplementedError(
            "SQLiteBackend does not support pub/sub. The dashboard will use polling instead."
        )
        yield  # makes the type checker treat this as an async generator


# ---------------------------------------------------------------------------
# Zero-arg factory for CLI use
#
# The CLI resolves SHIELD_BACKEND as a dotted-path factory:
#
#   SHIELD_BACKEND=examples.fastapi.custom_backend.sqlite_backend:make_backend
#
# This function reads SHIELD_SQLITE_PATH from the environment so the CLI can
# be configured entirely via env vars or a .shield file:
#
#   # .shield
#   SHIELD_BACKEND=examples.fastapi.custom_backend.sqlite_backend:make_backend
#   SHIELD_SQLITE_PATH=shield-state.db
# ---------------------------------------------------------------------------


def make_backend() -> SQLiteBackend:
    """Construct a ``SQLiteBackend`` from environment variables.

    Reads ``SHIELD_SQLITE_PATH`` (default: ``shield-state.db``).
    Called by the CLI when ``SHIELD_BACKEND`` is set to the dotted path
    of this function.
    """
    db_path = os.environ.get("SHIELD_SQLITE_PATH", "shield-state.db")
    return SQLiteBackend(db_path=db_path)


# ---------------------------------------------------------------------------
# Demo FastAPI app using SQLiteBackend
# ---------------------------------------------------------------------------

backend = SQLiteBackend("shield-state.db")
engine = ShieldEngine(backend=backend)
router = ShieldRouter(engine=engine)


@router.get("/health")
@force_active
async def health():
    """Always 200."""
    return {"status": "ok", "backend": "sqlite"}


@router.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC")
async def get_payments():
    """503 MAINTENANCE_MODE — state persisted in SQLite."""
    return {"payments": []}


@router.get("/legacy")
@disabled(reason="Use /payments instead")
async def legacy():
    """503 ROUTE_DISABLED — state persisted in SQLite."""
    return {}


@router.get("/orders")
async def get_orders():
    """200 active — no decorator."""
    return {"orders": [{"id": 1, "total": 49.99}]}


@router.get("/admin/status")
@force_active
async def admin_status():
    """All registered route states (read directly from SQLite)."""
    states = await engine.list_states()
    return {
        "backend": "sqlite",
        "db": "shield-state.db",
        "routes": [{"path": s.path, "status": s.status, "reason": s.reason} for s in states],
    }


@router.get("/admin/audit")
@force_active
async def admin_audit(limit: int = 20):
    """Audit log read from SQLite."""
    entries = await engine.get_audit_log(limit=limit)
    return {
        "entries": [
            {
                "timestamp": e.timestamp.isoformat(),
                "path": e.path,
                "action": e.action,
                "actor": e.actor,
                "previous_status": e.previous_status,
                "new_status": e.new_status,
            }
            for e in entries
        ]
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    # async with engine: calls backend.startup() then backend.shutdown()
    async with engine:
        yield


app = FastAPI(
    title="api-shield — SQLite Custom Backend Example",
    description=(
        "All route state and audit log entries are persisted in `shield-state.db`. "
        "Restart the server and the state survives."
    ),
    lifespan=lifespan,
)

app.add_middleware(ShieldMiddleware, engine=engine)
app.include_router(router)
apply_shield_to_openapi(app, engine)
