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

## Distributed support

The six abstract methods give you persistence. To unlock full distributed behaviour — live dashboard updates, cross-instance global maintenance sync, and webhook deduplication — implement three additional optional methods. Each one has a default that works correctly for single-instance deployments, so you can add them incrementally.

---

### The three distributed methods

```python
from collections.abc import AsyncIterator
from shield.core.backends.base import ShieldBackend

class MyDistributedBackend(ShieldBackend):

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Stream every per-route state change as it happens.

        Used by the dashboard SSE endpoint to push live updates to browsers
        without polling.  Yield a RouteState every time set_state() is called
        by any instance.  If your store does not support pub/sub, leave this
        unimplemented — the dashboard falls back to polling list_states()
        every few seconds automatically.
        """
        ...

    async def subscribe_global_config(self) -> AsyncIterator[None]:
        """Yield None whenever any instance writes a new global maintenance config.

        ShieldEngine keeps GlobalMaintenanceConfig in an in-process cache to
        avoid a storage round-trip on every request.  When another instance
        enables or disables global maintenance, it writes to the shared store
        and your implementation of this method should yield a signal so the
        engine drops its local cache and re-fetches on the next request.

        Yield None for each change signal — the content does not matter,
        only the arrival of the message.
        """
        ...

    async def try_claim_webhook_dispatch(
        self, dedup_key: str, ttl_seconds: int = 60
    ) -> bool:
        """Claim exclusive right to fire webhooks for one event.

        When a scheduled maintenance window activates, every instance
        independently calls set_maintenance() and would each fire all
        registered webhooks — producing N deliveries for one event.

        Before firing, ShieldEngine calls this method with a deterministic
        key derived from event + path + serialised RouteState (identical
        across all instances for the same event).  The first instance to
        win the claim fires; all others return False and skip.

        Use an atomic conditional write — "set this key only if it does not
        already exist" — and return True if you wrote it, False if it was
        already present.  Set the key to expire after ttl_seconds so that a
        crashed instance does not permanently suppress re-delivery.

        Return True unconditionally if your store does not support atomic
        conditional writes — webhooks will be over-delivered rather than
        silently dropped.
        """
        ...
```

All three raise `NotImplementedError` by default. The engine handles each gracefully:

| Method | What happens if not implemented |
|---|---|
| `subscribe()` | Dashboard SSE falls back to polling `list_states()` every few seconds |
| `subscribe_global_config()` | Global maintenance cache is per-process; stale until the process writes its own update |
| `try_claim_webhook_dispatch()` | Always returns `True` — every instance fires webhooks (over-delivery) |

---

### PostgreSQL example

PostgreSQL's `LISTEN` / `NOTIFY` is a built-in pub/sub mechanism that works across connections and processes — no extra broker needed.

```python
"""PostgreSQL distributed backend using asyncpg + LISTEN/NOTIFY.

pip install asyncpg
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg

from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, GlobalMaintenanceConfig, RouteState, RouteStatus


class PostgresBackend(ShieldBackend):

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shield_states (
                    path TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shield_audit (
                    id TEXT PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    path TEXT NOT NULL,
                    entry_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shield_webhook_dedup (
                    dedup_key TEXT PRIMARY KEY,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)

    async def shutdown(self) -> None:
        if self._pool:
            await self._pool.close()

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state_json FROM shield_states WHERE path = $1", path
            )
        if row is None:
            raise KeyError(path)
        return RouteState.model_validate_json(row["state_json"])

    async def set_state(self, path: str, state: RouteState) -> None:
        payload = state.model_dump_json()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shield_states (path, state_json) VALUES ($1, $2)
                ON CONFLICT (path) DO UPDATE SET state_json = EXCLUDED.state_json
                """,
                path, payload,
            )
            # Notify all listening instances of the per-route state change.
            await conn.execute("SELECT pg_notify('shield_changes', $1)", payload)

    async def delete_state(self, path: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM shield_states WHERE path = $1", path
            )

    async def list_states(self) -> list[RouteState]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT state_json FROM shield_states")
        return [RouteState.model_validate_json(r["state_json"]) for r in rows]

    async def write_audit(self, entry: AuditEntry) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shield_audit (id, ts, path, entry_json)
                VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING
                """,
                entry.id, entry.timestamp, entry.path, entry.model_dump_json(),
            )

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        async with self._pool.acquire() as conn:
            if path:
                rows = await conn.fetch(
                    "SELECT entry_json FROM shield_audit WHERE path = $1"
                    " ORDER BY ts DESC LIMIT $2",
                    path, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT entry_json FROM shield_audit ORDER BY ts DESC LIMIT $1",
                    limit,
                )
        return [AuditEntry.model_validate_json(r["entry_json"]) for r in rows]

    # ------------------------------------------------------------------
    # Distributed: per-route live updates (dashboard SSE)
    # ------------------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Stream RouteState changes via PostgreSQL LISTEN/NOTIFY."""
        queue: asyncio.Queue[RouteState] = asyncio.Queue()

        def _on_notify(conn, pid, channel, payload):
            try:
                state = RouteState.model_validate_json(payload)
                queue.put_nowait(state)
            except Exception:
                pass

        async with self._pool.acquire() as conn:
            await conn.add_listener("shield_changes", _on_notify)
            try:
                while True:
                    yield await queue.get()
            finally:
                await conn.remove_listener("shield_changes", _on_notify)

    # ------------------------------------------------------------------
    # Distributed: global maintenance cache invalidation
    # ------------------------------------------------------------------

    async def set_global_config(self, config: GlobalMaintenanceConfig) -> None:
        """Persist config and notify all instances to drop their cache."""
        await super().set_global_config(config)
        async with self._pool.acquire() as conn:
            # Empty string payload — only the arrival of the notification
            # matters, not its content.
            await conn.execute(
                "SELECT pg_notify('shield_global_invalidate', '1')"
            )

    async def subscribe_global_config(self) -> AsyncIterator[None]:
        """Yield None on each global config change via LISTEN/NOTIFY."""
        queue: asyncio.Queue[None] = asyncio.Queue()

        def _on_notify(conn, pid, channel, payload):
            queue.put_nowait(None)

        async with self._pool.acquire() as conn:
            await conn.add_listener("shield_global_invalidate", _on_notify)
            try:
                while True:
                    yield await queue.get()
            finally:
                await conn.remove_listener("shield_global_invalidate", _on_notify)

    # ------------------------------------------------------------------
    # Distributed: webhook deduplication
    # ------------------------------------------------------------------

    async def try_claim_webhook_dispatch(
        self, dedup_key: str, ttl_seconds: int = 60
    ) -> bool:
        """Claim webhook dispatch rights using an INSERT ... ON CONFLICT DO NOTHING.

        PostgreSQL's INSERT with ON CONFLICT is atomic — only one instance
        succeeds.  A background cleanup query removes expired rows so the
        table does not grow indefinitely.
        """
        async with self._pool.acquire() as conn:
            # Purge rows older than ttl_seconds first (best-effort cleanup).
            await conn.execute(
                "DELETE FROM shield_webhook_dedup"
                " WHERE claimed_at < now() - ($1 || ' seconds')::interval",
                str(ttl_seconds),
            )
            result = await conn.execute(
                "INSERT INTO shield_webhook_dedup (dedup_key)"
                " VALUES ($1) ON CONFLICT DO NOTHING",
                dedup_key,
            )
        # asyncpg returns "INSERT 0 1" when a row was inserted,
        # "INSERT 0 0" when ON CONFLICT suppressed the insert.
        return result == "INSERT 0 1"
```

---

### What your store needs to support each method

| Method | Minimum capability required |
|---|---|
| `subscribe()` | Pub/sub or change-data-capture (PostgreSQL `LISTEN/NOTIFY`, MySQL binlog, Kafka, NATS) |
| `subscribe_global_config()` | Same pub/sub as above — just a separate channel/topic |
| `try_claim_webhook_dispatch()` | Atomic conditional write — "insert only if absent" (SQL `INSERT … ON CONFLICT DO NOTHING`, DynamoDB `PutItem` with `attribute_not_exists`, etcd transactions, Zookeeper ephemeral nodes, Memcached `add`) |

---

## Building a framework adapter

If you want to support a framework other than FastAPI, the pattern is:

1. **Middleware** — catch `MaintenanceException`, `RouteDisabledException`, `EnvGatedException` from `engine.check()` and return appropriate responses.
2. **Route scanning** — at startup, iterate the framework's route list, detect `__shield_meta__`, and call `engine.register()`.
3. **Decorators** — reuse `shield.fastapi.decorators` as-is (they only stamp metadata; they are framework-agnostic).

The shield decorators, engine, and backends have zero framework dependencies and can power any adapter.
