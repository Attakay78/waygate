# Building Your Own Backend

Any storage layer can be used as a backend by subclassing `ShieldBackend`. api-shield handles everything else — the engine, middleware, decorators, CLI, and audit log all work unchanged.

---

## The contract

```python
from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState


class MyBackend(ShieldBackend):

    async def get_state(self, path: str) -> RouteState:
        """Return stored state. MUST raise KeyError if path not found."""
        ...

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist state for path, overwriting any existing entry."""
        ...

    async def delete_state(self, path: str) -> None:
        """Remove state for path. No-op if not found."""
        ...

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        ...

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append entry to the audit log."""
        ...

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """Return audit entries newest-first, optionally filtered by path."""
        ...
```

### Rules

| Rule | Detail |
|---|---|
| `get_state()` must raise `KeyError` | Engine uses `KeyError` to distinguish "not registered" from "registered but active" |
| Fail-open on errors | Let exceptions bubble up — `ShieldEngine` wraps every backend call and allows requests through on failure |
| Thread safety | All methods are async; use your storage library's async client where available |
| `subscribe()` is optional | Default raises `NotImplementedError`; dashboard SSE falls back to polling |
| Global maintenance | Inherited from `ShieldBackend` base — no extra work unless you want a dedicated storage path |

---

## Serialisation

Use Pydantic's built-in helpers to convert models to/from JSON:

```python
# RouteState → JSON string
json_str = state.model_dump_json()

# JSON string → RouteState
state = RouteState.model_validate_json(json_str)

# AuditEntry → dict
entry_dict = entry.model_dump(mode="json")

# dict → AuditEntry
entry = AuditEntry.model_validate(entry_dict)
```

---

## Lifecycle hooks

Override `startup()` and `shutdown()` for connection setup/teardown:

```python
class MyBackend(ShieldBackend):
    async def startup(self) -> None:
        self._conn = await connect_to_db()

    async def shutdown(self) -> None:
        await self._conn.close()
```

These are called automatically when you use `async with engine:` in your FastAPI lifespan.

---

## Full example: SQLite backend

A complete working implementation backed by SQLite (requires `pip install aiosqlite`):

```python
"""SQLite backend for api-shield.

Usage:
    pip install aiosqlite
    uv run uvicorn examples.fastapi.custom_backend.sqlite_backend:app --reload
"""

import aiosqlite

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState


class SQLiteBackend(ShieldBackend):
    def __init__(self, db_path: str = "shield-state.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def startup(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS shield_states (
                path TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS shield_audit (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                path TEXT NOT NULL,
                entry_json TEXT NOT NULL
            )
        """)
        await self._db.commit()

    async def shutdown(self) -> None:
        if self._db:
            await self._db.close()

    async def get_state(self, path: str) -> RouteState:
        assert self._db is not None
        async with self._db.execute(
            "SELECT state_json FROM shield_states WHERE path = ?", (path,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(path)   # ← required contract
        return RouteState.model_validate_json(row[0])

    async def set_state(self, path: str, state: RouteState) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO shield_states VALUES (?, ?)"
            " ON CONFLICT(path) DO UPDATE SET state_json = excluded.state_json",
            (path, state.model_dump_json()),
        )
        await self._db.commit()

    async def delete_state(self, path: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM shield_states WHERE path = ?", (path,)
        )
        await self._db.commit()

    async def list_states(self) -> list[RouteState]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT state_json FROM shield_states"
        ) as cur:
            rows = await cur.fetchall()
        return [RouteState.model_validate_json(row[0]) for row in rows]

    async def write_audit(self, entry: AuditEntry) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR IGNORE INTO shield_audit VALUES (?, ?, ?, ?)",
            (entry.id, entry.timestamp.isoformat(), entry.path, entry.model_dump_json()),
        )
        await self._db.commit()

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        assert self._db is not None
        if path:
            query = "SELECT entry_json FROM shield_audit WHERE path = ? ORDER BY timestamp DESC LIMIT ?"
            params: tuple = (path, limit)
        else:
            query = "SELECT entry_json FROM shield_audit ORDER BY timestamp DESC LIMIT ?"
            params = (limit,)
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [AuditEntry.model_validate_json(row[0]) for row in rows]
```

### Wire it to the engine

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from shield.core.engine import ShieldEngine
from shield.fastapi.middleware import ShieldMiddleware
from shield.admin import ShieldAdmin

backend = SQLiteBackend(db_path="shield-state.db")
engine = ShieldEngine(backend=backend)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:   # → backend.startup() … backend.shutdown()
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ShieldMiddleware, engine=engine)
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

Everything works from here — decorators, CLI, dashboard, audit log — with SQLite as the storage layer.

---

## Building a framework adapter

If you want to support a framework other than FastAPI, the pattern is:

1. **Middleware** — catch `MaintenanceException`, `RouteDisabledException`, `EnvGatedException` from `engine.check()` and return appropriate responses.
2. **Route scanning** — at startup, iterate the framework's route list, detect `__shield_meta__`, and call `engine.register()`.
3. **Decorators** — reuse `shield.fastapi.decorators` as-is (they only stamp metadata; they are framework-agnostic).

The shield decorators, engine, and backends have zero framework dependencies and can power any adapter.
