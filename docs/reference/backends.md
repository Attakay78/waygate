# Backends

All backends implement the `ShieldBackend` abstract base class. You can swap backends with a one-line change — all other code remains unchanged.

---

## ShieldBackend (ABC)

::: shield.core.backends.base.ShieldBackend
    options:
      show_source: false

---

## MemoryBackend

::: shield.core.backends.memory.MemoryBackend
    options:
      show_source: false

**Usage:**

```python
from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=MemoryBackend())
```

- State is stored in a Python `dict` — lost on restart.
- `subscribe()` is implemented via `asyncio.Queue` for SSE live updates.
- Audit log capped at 1000 entries.

---

## FileBackend

::: shield.core.backends.file.FileBackend
    options:
      show_source: false

**Usage:**

```python
from shield.core.backends.file import FileBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=FileBackend(path="shield-state.json"))
```

- Reads/writes a JSON file via `aiofiles`.
- File lock (`asyncio.Lock`) prevents concurrent write corruption.
- `subscribe()` raises `NotImplementedError` — dashboard falls back to polling.

**File format:**

```json
{
  "states": {
    "GET:/payments": { "path": "GET:/payments", "status": "maintenance", ... }
  },
  "audit": [ ... ]
}
```

---

## RedisBackend

::: shield.core.backends.redis.RedisBackend
    options:
      show_source: false

**Usage:**

```bash
uv add "api-shield[redis]"
```

```python
from shield.core.backends.redis import RedisBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=RedisBackend(url="redis://localhost:6379/0"))
```

Key schema:

| Key | Redis type | Description |
|---|---|---|
| `shield:state:{path}` | String | JSON-serialised `RouteState` |
| `shield:audit` | List | JSON-serialised `AuditEntry` items (LPUSH, LTRIM to 1000) |
| `shield:global` | String | JSON-serialised `GlobalMaintenanceConfig` |
| `shield:changes` | Pub/sub | Published on every `set_state()` for SSE live updates |

- `subscribe()` is implemented via Redis pub/sub on `shield:changes`.
- Connection pooling via `redis.asyncio.ConnectionPool`.
- Redis connection errors are handled gracefully (fail-open).

---

## Writing a custom backend

Subclass `ShieldBackend` and implement the six required async methods:

```python
from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState


class MyBackend(ShieldBackend):

    async def get_state(self, path: str) -> RouteState:
        """Return stored state. MUST raise KeyError if path not found."""
        ...

    async def set_state(self, path: str, state: RouteState) -> None:
        """Persist state, overwriting any existing entry."""
        ...

    async def delete_state(self, path: str) -> None:
        """Remove state. No-op if not found."""
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

### Contract rules

| Rule | Detail |
|---|---|
| `get_state()` must raise `KeyError` | Engine distinguishes "not registered" from "registered but active" via this exception |
| Fail-open on errors | Let exceptions bubble up — `ShieldEngine` wraps every backend call |
| Thread safety | All methods are async; use your storage library's async client |
| `subscribe()` is optional | Default raises `NotImplementedError`; dashboard falls back to polling |

### Serialisation helpers

```python
# Serialise to JSON string
json_str = state.model_dump_json()

# Deserialise from JSON string
state = RouteState.model_validate_json(json_str)
```

### Lifecycle hooks

Override `startup()` and `shutdown()` for connection setup/teardown:

```python
class MyBackend(ShieldBackend):
    async def startup(self) -> None:
        self._conn = await connect()

    async def shutdown(self) -> None:
        await self._conn.close()
```

See [**Building your own backend →**](../adapters/custom.md) for a complete SQLite example.
