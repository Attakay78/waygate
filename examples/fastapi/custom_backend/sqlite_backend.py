"""Custom Backend Example — SQLite via aiosqlite.

This file shows how to wire waygate to a storage layer it does not ship
with by implementing the ``WaygateBackend`` abstract base class.

The contract is simple: implement six async methods and waygate handles the
rest (engine logic, middleware, decorators, dashboard, CLI, audit log).

Requirements:
    pip install aiosqlite
    # or: uv add aiosqlite

Run the demo app:
    uv run uvicorn examples.fastapi.custom_backend.sqlite_backend:app --reload

Then visit:
    http://localhost:8000/docs           — filtered Swagger UI
    http://localhost:8000/waygate/        — admin dashboard (login: admin / secret)
    http://localhost:8000/waygate/audit   — audit log

CLI quick-start (the CLI talks to the app's admin API — it never touches
the database directly):
    waygate config set-url http://localhost:8000/waygate
    waygate login admin          # password: secret
    waygate status
    waygate disable GET:/payments --reason "hotfix"
    waygate enable GET:/payments
    waygate log
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI

from waygate import AuditEntry, RouteState, WaygateBackend, WaygateEngine
from waygate.fastapi import (
    WaygateAdmin,
    WaygateMiddleware,
    WaygateRouter,
    apply_waygate_to_openapi,
    disabled,
    force_active,
    maintenance,
)

# ---------------------------------------------------------------------------
# SQLiteBackend — implements the WaygateBackend contract
#
# Rules to follow when building any custom backend:
#
#   1. Subclass ``WaygateBackend`` from ``waygate.core.backends.base``.
#   2. Implement all six @abstractmethod methods.
#   3. Override ``startup()`` / ``shutdown()`` for async initialisation —
#      the engine calls these automatically when used as ``async with engine:``.
#   4. RouteState and AuditEntry are Pydantic models — use .model_dump_json()
#      to serialise and .model_validate_json() to deserialise.
#   5. get_state() must raise KeyError when the path is not found.
#   6. Fail-open: let exceptions bubble up — WaygateEngine wraps every backend
#      call in try/except and allows the request through on failure.
#   7. subscribe() is optional. Leave it as-is if your backend doesn't support
#      pub/sub (the base class raises NotImplementedError and the dashboard
#      falls back to polling).
# ---------------------------------------------------------------------------

_MAX_AUDIT_ROWS = 1000

_CREATE_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS waygate_states (
    path      TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
)
"""

_CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS waygate_audit (
    id           TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    path         TEXT NOT NULL,
    entry_json   TEXT NOT NULL
)
"""


class SQLiteBackend(WaygateBackend):
    """waygate backend backed by a SQLite database.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Use ``:memory:`` for an in-process
        database (useful for tests — not shared across processes).

    Example
    -------
    >>> backend = SQLiteBackend("waygate-state.db")
    >>> engine  = WaygateEngine(backend=backend)
    >>> async with engine:           # calls startup() then shutdown()
    ...     states = await engine.list_states()
    """

    def __init__(self, db_path: str = "waygate-state.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle hooks — called automatically by WaygateEngine
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open the database connection and create tables if needed.

        Called automatically by ``WaygateEngine.__aenter__``.  You do not
        need to call this yourself when using ``async with engine:``.
        """
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = sqlite3.Row
        await self._db.execute(_CREATE_STATES_TABLE)
        await self._db.execute(_CREATE_AUDIT_TABLE)
        await self._db.commit()

    async def shutdown(self) -> None:
        """Close the database connection.

        Called automatically by ``WaygateEngine.__aexit__``.
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
    # WaygateBackend — required methods
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the stored state for *path*.

        Raises ``KeyError`` if the path has not been registered yet —
        this is the contract the engine relies on to distinguish
        "not registered" from "registered but active".
        """
        async with self._conn.execute(
            "SELECT state_json FROM waygate_states WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(f"No state registered for path {path!r}")

        return RouteState.model_validate_json(row["state_json"])

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist *state* for *path*, creating or replacing the existing row."""
        await self._conn.execute(
            """
            INSERT INTO waygate_states (path, state_json)
            VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET state_json = excluded.state_json
            """,
            (path, state.model_dump_json()),
        )
        await self._conn.commit()

    async def delete_state(self, path: str) -> None:
        """Remove the state row for *path*. No-op if not found."""
        await self._conn.execute("DELETE FROM waygate_states WHERE path = ?", (path,))
        await self._conn.commit()

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        async with self._conn.execute("SELECT state_json FROM waygate_states") as cursor:
            rows = await cursor.fetchall()
        return [RouteState.model_validate_json(row["state_json"]) for row in rows]

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append *entry* to the audit log, capping the table at 1000 rows."""
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO waygate_audit (id, timestamp, path, entry_json)
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
            DELETE FROM waygate_audit
            WHERE id NOT IN (
                SELECT id FROM waygate_audit
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
                SELECT entry_json FROM waygate_audit
                WHERE path = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (path, limit)
        else:
            query = """
                SELECT entry_json FROM waygate_audit
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
# Demo FastAPI app using SQLiteBackend
# ---------------------------------------------------------------------------

backend = SQLiteBackend("waygate-state.db")
engine = WaygateEngine(backend=backend)
router = WaygateRouter(engine=engine)


@router.get("/health")
@force_active
async def health():
    """Always 200 — bypasses every waygate check."""
    return {"status": "ok", "backend": "sqlite"}


@router.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC")
async def get_payments():
    """Returns 503 MAINTENANCE_MODE — state persisted in SQLite."""
    return {"payments": []}


@router.get("/legacy")
@disabled(reason="Use /payments instead")
async def legacy():
    """Returns 503 ROUTE_DISABLED — state persisted in SQLite."""
    return {}


@router.get("/orders")
async def get_orders():
    """200 active — no decorator, state persists across restarts via SQLite."""
    return {"orders": [{"id": 1, "total": 49.99}]}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # async with engine: calls backend.startup() then backend.shutdown()
    async with engine:
        yield


app = FastAPI(
    title="waygate — SQLite Custom Backend Example",
    description=(
        "All route state and audit log entries are persisted in `waygate-state.db`. "
        "Restart the server and the state survives.\n\n"
        "Admin UI and CLI API available at `/waygate/`."
    ),
    lifespan=lifespan,
)

app.add_middleware(WaygateMiddleware, engine=engine)
app.include_router(router)
apply_waygate_to_openapi(app, engine)

# Mount the unified admin interface:
#   - Dashboard UI  → http://localhost:8000/waygate/
#   - REST API      → http://localhost:8000/waygate/api/...  (used by the CLI)
#
# The CLI communicates with the app via this REST API — it never touches
# the SQLite database directly.  This means the same CLI workflow works
# regardless of which backend the app uses.
app.mount(
    "/waygate",
    WaygateAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/waygate",
        # secret_key="change-me-in-production",
        # token_expiry=86400,  # seconds — default 24 h
    ),
)
