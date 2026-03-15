# ShieldEngine

`ShieldEngine` is the central orchestrator. All state management logic lives here — middleware, decorators, the CLI, and the dashboard are all transport layers that delegate to the engine.

```python
from shield.core.engine import ShieldEngine
from shield.core.backends.memory import MemoryBackend

engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
```

Or use `make_engine()` to read configuration from environment variables and the `.shield` file:

```python
from shield.core.config import make_engine

engine = make_engine()
```

---

## Constructor

```python
ShieldEngine(
    backend: ShieldBackend | None = None,
    current_env: str = "production",
    webhooks: list[str] | None = None,
)
```

| Parameter | Default | Description |
|---|---|---|
| `backend` | `MemoryBackend()` | Storage backend for route state and audit log |
| `current_env` | `"production"` | Current environment name — used by `@env_only` checks |
| `webhooks` | `[]` | List of webhook URLs to notify on state changes |

---

## Route state methods

### `check`

```python
async def check(path: str, method: str = "") -> None
```

The single enforcement chokepoint. Called by `ShieldMiddleware` on every request. Raises an exception if the route is blocked; returns `None` if it should proceed.

Raises:
- `MaintenanceException` — route is in maintenance mode
- `RouteDisabledException` — route is disabled
- `EnvGatedException` — route is env-gated and current env is not allowed
- Never raises on backend errors — fail-open, logs the error instead

### `register`

```python
async def register(path: str, meta: dict) -> None
```

Called by `ShieldRouter` at startup to register a route's initial state from `__shield_meta__`. If the backend already has a state for this path, the persisted state wins.

### `enable`

```python
async def enable(path: str, actor: str = "system") -> RouteState
```

Set a route to `ACTIVE`. Works even if the route was disabled or in maintenance.

```python
await engine.enable("GET:/payments", actor="alice")
```

### `disable`

```python
async def disable(path: str, reason: str = "", actor: str = "system") -> RouteState
```

Set a route to `DISABLED`.

```python
await engine.disable("GET:/payments", reason="Feature removed", actor="alice")
```

### `set_maintenance`

```python
async def set_maintenance(
    path: str,
    reason: str = "",
    window: MaintenanceWindow | None = None,
    actor: str = "system",
) -> RouteState
```

Set a route to `MAINTENANCE`. If `window` is provided, the scheduler will auto-activate at `window.start` and auto-deactivate at `window.end`.

```python
from shield.core.models import MaintenanceWindow
from datetime import datetime, UTC

await engine.set_maintenance(
    "GET:/payments",
    reason="DB migration",
    window=MaintenanceWindow(
        start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
        end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
        reason="Planned migration window",
    ),
    actor="alice",
)
```

### `set_env_only`

```python
async def set_env_only(
    path: str,
    envs: list[str],
    actor: str = "system",
) -> RouteState
```

Restrict a route to the given environments. Returns 404 in all other envs.

```python
await engine.set_env_only("GET:/debug", envs=["dev", "staging"])
```

### `get_state`

```python
async def get_state(path: str) -> RouteState
```

Retrieve the current state of a route. Raises `KeyError` if the path has not been registered.

### `list_states`

```python
async def list_states() -> list[RouteState]
```

Return all registered route states.

---

## Scheduled maintenance

### `schedule_maintenance`

```python
async def schedule_maintenance(path: str, window: MaintenanceWindow) -> None
```

Schedule a future maintenance window. Creates an `asyncio.Task` that activates maintenance at `window.start` and restores `ACTIVE` at `window.end`.

```python
await engine.schedule_maintenance("GET:/payments", window=window)
```

### `cancel_schedule`

```python
async def cancel_schedule(path: str) -> None
```

Cancel any pending scheduled window for the given path.

---

## Global maintenance

### `enable_global_maintenance`

```python
async def enable_global_maintenance(
    reason: str = "",
    exempt_paths: list[str] | None = None,
    include_force_active: bool = False,
    actor: str = "system",
) -> None
```

Block every route at once. Exempt paths bypass the global check and respond normally.

```python
await engine.enable_global_maintenance(
    reason="Emergency patch — back in 15 minutes",
    exempt_paths=["/health", "GET:/admin/status"],
    include_force_active=False,
)
```

Exempt path formats:

- `/health` — matches the path for any HTTP method
- `GET:/health` — matches only `GET /health`

### `disable_global_maintenance`

```python
async def disable_global_maintenance(actor: str = "system") -> None
```

Restore all routes to their individual states.

### `get_global_maintenance`

```python
async def get_global_maintenance() -> GlobalMaintenanceConfig
```

Return the current global maintenance config (enabled state, reason, exempt paths).

### `set_global_exempt_paths`

```python
async def set_global_exempt_paths(paths: list[str], actor: str = "system") -> None
```

Update the exempt path list while global maintenance is active, without toggling the mode.

---

## Audit log

### `get_audit_log`

```python
async def get_audit_log(
    path: str | None = None,
    limit: int = 100,
) -> list[AuditEntry]
```

Return audit entries newest-first. Optionally filter by route path.

```python
entries = await engine.get_audit_log(limit=50)
entries = await engine.get_audit_log(path="GET:/payments", limit=20)
```

---

## Webhooks

### `add_webhook`

```python
def add_webhook(url: str, formatter=None) -> None
```

Register a webhook URL to be notified on every state change. `formatter` can be `None` (default JSON) or `SlackWebhookFormatter()`.

```python
from shield.core.webhooks import SlackWebhookFormatter

engine.add_webhook("https://hooks.slack.com/services/...", formatter=SlackWebhookFormatter())
engine.add_webhook("https://my-service.example.com/shield-events")
```

Webhook payload (default formatter):

```json
{
  "event": "maintenance_on",
  "path": "GET:/payments",
  "reason": "DB migration",
  "timestamp": "2025-06-01T02:00:00Z",
  "state": { "path": "GET:/payments", "status": "maintenance", ... }
}
```

Webhook failures are logged and never affect the request path.

---

## Lifecycle (async context manager)

Use `async with engine:` in your FastAPI lifespan to call `backend.startup()` and `backend.shutdown()` automatically:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:
        yield

app = FastAPI(lifespan=lifespan)
```

---

## Fail-open guarantee

If the backend raises any exception during `engine.check()`, the error is logged and the request is **allowed through**. api-shield never takes down your API due to its own backend being unreachable.
