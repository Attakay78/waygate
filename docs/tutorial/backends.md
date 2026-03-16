# Backends

A backend is where api-shield stores route state and the audit log. Swapping backends requires a one-line change; everything else (decorators, middleware, CLI, audit log) works unchanged.

---

## Choosing a backend

| Backend | Persistence | Multi-instance | Best for |
|---|---|---|---|
| `MemoryBackend` | No | No | Development, testing |
| `FileBackend` | Yes | No (single process) | Simple single-instance deployments |
| `RedisBackend` | Yes | Yes | Production, load-balanced |
| Custom | You decide | You decide | Any other storage layer |

---

## MemoryBackend (default)

State lives in a Python `dict`. Lost on restart. The CLI cannot share state with the running server unless it also uses the in-process engine (e.g. via the admin API).

```python
from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=MemoryBackend())
```

Best for: development, unit tests, demos.

---

## FileBackend

State is written to a JSON file on disk. The CLI and the running server share state as long as both point to the same file.

```python
from shield.core.backends.file import FileBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=FileBackend(path="shield-state.json"))
```

Or via environment variables:

```bash
SHIELD_BACKEND=file SHIELD_FILE_PATH=./shield-state.json uvicorn app:app
```

File format:

```json
{
  "states": {
    "GET:/payments": { "path": "GET:/payments", "status": "maintenance", ... }
  },
  "audit": [...]
}
```

Best for: single-instance deployments, CLI-driven workflows.

---

## RedisBackend

State is stored in Redis. All instances in a deployment share the same state. Pub/sub keeps the dashboard SSE feed live across instances.

```bash
uv add "api-shield[redis]"
```

```python
from shield.core.backends.redis import RedisBackend
from shield.core.engine import ShieldEngine

engine = ShieldEngine(backend=RedisBackend(url="redis://localhost:6379/0"))
```

Or via environment variable:

```bash
SHIELD_BACKEND=redis SHIELD_REDIS_URL=redis://localhost:6379/0 uvicorn app:app
```

Redis key schema:

| Key | Type | Description |
|---|---|---|
| `shield:state:{path}` | String | JSON-serialised `RouteState` |
| `shield:audit` | List | JSON-serialised `AuditEntry` items (capped at 1000) |
| `shield:global` | String | JSON-serialised global maintenance config |
| `shield:changes` | Pub/sub channel | Publishes on every `set_state` — used by SSE |

Best for: multi-instance / load-balanced production deployments.

---

## Using `make_engine` (recommended)

`make_engine()` reads `SHIELD_BACKEND` (and related env vars) so you never hardcode the backend:

```python
from shield.core.config import make_engine

engine = make_engine()                           # reads env + .shield file
engine = make_engine(current_env="staging")      # override env
engine = make_engine(backend="redis")            # force backend type
```

This lets you use `MemoryBackend` locally and `RedisBackend` in production without touching your app code.

---

## Custom backends

Any storage layer can be used by subclassing `ShieldBackend`:

```python
from shield.core.backends.base import ShieldBackend
from shield.core.models import AuditEntry, RouteState

class MyBackend(ShieldBackend):

    async def get_state(self, path: str) -> RouteState:
        # MUST raise KeyError if not found
        ...

    async def set_state(self, path: str, state: RouteState) -> None:
        ...

    async def delete_state(self, path: str) -> None:
        ...

    async def list_states(self) -> list[RouteState]:
        ...

    async def write_audit(self, entry: AuditEntry) -> None:
        ...

    async def get_audit_log(
        self, path: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        ...
```

See [**Adapters: Building your own backend →**](../adapters/custom.md) for a full SQLite example.

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

Use `async with engine:` in your app lifespan to call them automatically:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:   # → backend.startup() … backend.shutdown()
        yield

app = FastAPI(lifespan=lifespan)
```

---

## Next step

[**Tutorial: Admin dashboard →**](admin-dashboard.md)
