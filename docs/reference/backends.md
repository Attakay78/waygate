# Backends

A backend is the storage layer for api-shield. It persists route state and the audit log. All backends implement the `ShieldBackend` abstract base class, so you can swap them with a one-line change and nothing else in your application needs to change.

```python
from shield.core.backends.base import ShieldBackend
```

| Backend | Storage | Survives restart | Live push (SSE) | Best for |
|---|---|---|---|---|
| `MemoryBackend` | In-process dict | No | Yes (asyncio.Queue) | Development, testing |
| `FileBackend` | JSON file on disk | Yes | No (polling fallback) | Single-instance, simple deployments |
| `RedisBackend` | Redis | Yes | Yes (pub/sub) | Multi-instance, production |

---

## ShieldBackend (ABC)

The contract all backends must implement. If you are building a custom backend, subclass this.

::: shield.core.backends.base.ShieldBackend
    options:
      show_source: false

---

## MemoryBackend

Stores all state in a Python `dict` in the current process. No installation required and no configuration needed — the default choice for getting started.

::: shield.core.backends.memory.MemoryBackend
    options:
      show_source: false

### Usage

```python title="main.py"
from shield.core.engine import ShieldEngine

# MemoryBackend is the default — no need to pass it explicitly
engine = ShieldEngine()

# Or explicitly
from shield.core.backends.memory import MemoryBackend
engine = ShieldEngine(backend=MemoryBackend())
```

### Characteristics

- State is stored in a Python `dict` and lost on process restart.
- `subscribe()` is implemented via `asyncio.Queue`, enabling live SSE updates in the admin dashboard.
- The audit log is capped at 1000 entries (oldest entries are discarded).

!!! warning "Not for production"
    `MemoryBackend` state is reset every time the process restarts. If you restart your server, all runtime state changes (routes disabled via CLI, maintenance mode set via dashboard) are lost. Use `FileBackend` or `RedisBackend` in production.

---

## FileBackend

Reads and writes a JSON file using `aiofiles`. State survives process restarts and can be shared between processes on the same machine by pointing them at the same file.

::: shield.core.backends.file.FileBackend
    options:
      show_source: false

### Usage

```python title="main.py"
from shield.core.backends.file import FileBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=FileBackend(path="shield-state.json"))
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | File path for the JSON state file. Relative to the working directory of the process. |

### File format

```json
{
  "states": {
    "GET:/payments": {
      "path": "GET:/payments",
      "status": "maintenance",
      "reason": "DB migration",
      "window": null
    }
  },
  "audit": [
    {
      "id": "...",
      "timestamp": "2025-06-01T02:00:00Z",
      "path": "GET:/payments",
      "action": "maintenance",
      "actor": "alice",
      "platform": "cli"
    }
  ]
}
```

### Characteristics

- File writes go through an `asyncio.Lock` to prevent concurrent write corruption.
- `subscribe()` raises `NotImplementedError`; the admin dashboard falls back to polling every few seconds.
- Supports JSON, YAML, and TOML files — the format is detected from the file extension.

!!! tip "Commit the `.gitignore` entry"
    Add the state file path to `.gitignore` to avoid accidentally committing runtime state to version control. The file is machine-generated and changes frequently.

---

## RedisBackend

Uses `redis.asyncio` for fully async, multi-instance deployments. State is shared across all app instances, and the `subscribe()` method enables real-time SSE push in the admin dashboard via Redis pub/sub.

::: shield.core.backends.redis.RedisBackend
    options:
      show_source: false

### Installation

```bash
uv add "api-shield[redis]"
```

### Usage

```python title="main.py"
from shield.core.backends.redis import RedisBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=RedisBackend(url="redis://localhost:6379/0"))
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | required | Redis connection URL. Supports `redis://`, `rediss://` (TLS), and `redis+unix://`. |

### Key schema

| Redis key | Type | Contents |
|---|---|---|
| `shield:state:{path}` | String | JSON-serialised `RouteState` |
| `shield:audit` | List | JSON-serialised `AuditEntry` items (LPUSH, LTRIM to 1000) |
| `shield:global` | String | JSON-serialised `GlobalMaintenanceConfig` |
| `shield:changes` | Pub/sub channel | Published on every `set_state()` for SSE live updates |

### Characteristics

- `subscribe()` is implemented via Redis pub/sub on `shield:changes`, enabling live SSE updates in the admin dashboard.
- Uses connection pooling via `redis.asyncio.ConnectionPool` for efficiency under load.
- Redis connection errors are handled gracefully — the backend surfaces them as exceptions and the engine fails open. Read more in [ShieldEngine: fail-open](engine.md#check).

!!! tip "Use the lifespan context manager"
    `RedisBackend` opens a connection pool on `startup()` and closes it on `shutdown()`. Always wrap the engine in the lifespan context manager to ensure clean teardown. Read more in [ShieldEngine: lifecycle](engine.md#lifecycle).

---

## Writing a custom backend

Subclass `ShieldBackend` and implement the six required async methods. The contract is intentionally minimal.

```python title="my_backend.py"
from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState


class MyBackend(ShieldBackend):

    async def get_state(self, path: str) -> RouteState:
        """Return stored state. Must raise KeyError if path is not found."""
        ...

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist state, overwriting any existing entry for this path."""
        ...

    async def delete_state(self, path: str) -> None:
        """Remove state for this path. No-op if not found."""
        ...

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states."""
        ...

    async def write_audit(self, entry: AuditEntry) -> None:
        """Append an entry to the audit log."""
        ...

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """Return audit entries newest-first, optionally filtered by path."""
        ...
```

??? info "Contract rules"

    | Rule | Detail |
    |---|---|
    | `get_state()` must raise `KeyError` | The engine uses this to distinguish "not registered" from "registered but active" |
    | Let errors bubble up | The engine wraps every backend call and handles errors (fail-open). Do not swallow exceptions. |
    | All methods must be async | Use your storage library's async client to avoid blocking the event loop |
    | `subscribe()` is optional | Override it if your storage supports pub/sub; otherwise the default raises `NotImplementedError` and the dashboard falls back to polling |

??? example "Serialisation helpers"

    Use the Pydantic v2 model methods to convert between `RouteState`/`AuditEntry` and JSON:

    ```python
    # Serialise to a JSON string
    json_str = state.model_dump_json()

    # Deserialise from a JSON string
    state = RouteState.model_validate_json(json_str)
    ```

??? example "Lifecycle hooks (startup and shutdown)"

    Override `startup()` and `shutdown()` to manage connections:

    ```python
    class MyBackend(ShieldBackend):

        async def startup(self) -> None:
            self._conn = await create_connection()

        async def shutdown(self) -> None:
            await self._conn.close()
    ```

See [Building your own backend](../adapters/custom.md) for a complete working SQLite example.
