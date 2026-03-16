# ShieldEngine

`ShieldEngine` is the central orchestrator of api-shield. All state management logic lives here — middleware, decorators, the CLI, and the dashboard are transport layers that delegate to the engine. If you are building a custom adapter or automating route management, this is the class you interact with directly.

```python
from shield.core.engine import ShieldEngine
```

---

## Quick start

=== "Default (in-memory)"

    ```python title="main.py"
    from shield.core.engine import ShieldEngine

    engine = ShieldEngine()
    ```

=== "With a backend"

    ```python title="main.py"
    from shield.core.engine import ShieldEngine
    from shield.core.backends.memory import MemoryBackend

    engine = ShieldEngine(backend=MemoryBackend(), current_env="production")
    ```

=== "From config / env vars"

    ```python title="main.py"
    from shield.core.config import make_engine

    engine = make_engine()  # reads SHIELD_BACKEND, SHIELD_ENV, etc.
    ```

!!! tip "Use `make_engine()` in production"
    `make_engine()` reads configuration from environment variables and your `.shield` file, so you can swap backends without changing application code. See [Configuration](../guides/production.md) for details.

---

## Constructor

```python
ShieldEngine(
    backend: ShieldBackend | None = None,
    current_env: str = "dev",
    webhooks: list[str] | None = None,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `backend` | `ShieldBackend \| None` | `MemoryBackend()` | Storage backend for route state and the audit log. Read more in [Backends](backends.md). |
| `current_env` | `str` | `"dev"` | The current environment name. Used by `@env_only` to decide whether to allow or block a request. |
| `webhooks` | `list[str] \| None` | `[]` | Webhook URLs notified on every state change. Read more in [add_webhook](#add_webhook). |

---

## Lifecycle

### Using as an async context manager

Wrap the engine in your FastAPI lifespan to ensure the backend connects and disconnects cleanly:

```python title="main.py"
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:  # calls backend.startup() / backend.shutdown()
        yield

app = FastAPI(lifespan=lifespan)
```

!!! warning "Always use the lifespan with Redis"
    `RedisBackend` opens a connection pool on `startup()` and closes it on `shutdown()`. Without the lifespan wrapper, connections leak on shutdown.

---

## Route state methods

### `check`

```python
async def check(path: str, method: str = "") -> None
```

The single enforcement chokepoint. Called by `ShieldMiddleware` on every request. Raises a `ShieldException` subclass if the route is blocked; returns `None` if it may proceed.

??? info "Resolution order"

    1. Global maintenance enabled and path not exempt → raise `MaintenanceException`
    2. Route has `force_active=True` → return immediately (always allow)
    3. Route status is `MAINTENANCE` → raise `MaintenanceException`
    4. Route status is `DISABLED` → raise `RouteDisabledException`
    5. Route status is `ENV_GATED` and current env not allowed → raise `EnvGatedException`
    6. Route status is `ACTIVE` or `DEPRECATED` → return `None`

!!! note "Fail-open on backend errors"
    If the backend raises any exception, `check()` logs the error and returns `None`, allowing the request through. api-shield never takes down your API because its own backend is unreachable.

**Raises:**

| Exception | When |
|---|---|
| `MaintenanceException` | Route (or global maintenance) is active |
| `RouteDisabledException` | Route is permanently disabled |
| `EnvGatedException` | Route is restricted and the current env is not in `allowed_envs` |

Read more in [Exceptions](exceptions.md).

---

### `register`

```python
async def register(path: str, meta: dict) -> None
```

Register a route's initial state from its `__shield_meta__` dictionary. Called by `ShieldRouter` at startup — you rarely need to call this directly.

**Persistence-first semantics:** if the backend already has a saved state for this path (from a previous run), the persisted state wins over the decorator metadata. This means a route you manually disabled via the CLI stays disabled after a restart.

---

### `enable`

```python
async def enable(path: str, actor: str = "system") -> RouteState
```

Set a route to `ACTIVE`. Works regardless of the current status.

```python title="example"
await engine.enable("GET:/payments", actor="alice")
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Route key, e.g. `"GET:/payments"` |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |

---

### `disable`

```python
async def disable(path: str, reason: str = "", actor: str = "system") -> RouteState
```

Set a route to `DISABLED`. Returns 503 to all callers.

```python title="example"
await engine.disable("GET:/payments", reason="Feature removed", actor="alice")
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Route key |
| `reason` | `str` | `""` | Shown in the 503 error response and recorded in the audit log |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |

---

### `set_maintenance`

```python
async def set_maintenance(
    path: str,
    reason: str = "",
    window: MaintenanceWindow | None = None,
    actor: str = "system",
) -> RouteState
```

Set a route to `MAINTENANCE`. If `window` is provided, the scheduler auto-activates at `window.start` and auto-deactivates at `window.end`.

??? example "Example: scheduled maintenance window"

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

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Route key |
| `reason` | `str` | `""` | Shown in the 503 error response |
| `window` | `MaintenanceWindow \| None` | `None` | Optional scheduled window. Read more in [MaintenanceWindow](models.md#maintenancewindow). |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |

---

### `set_env_only`

```python
async def set_env_only(
    path: str,
    envs: list[str],
    actor: str = "system",
) -> RouteState
```

Restrict a route to the listed environments. Returns a silent 404 in all other environments.

```python title="example"
await engine.set_env_only("GET:/debug", envs=["dev", "staging"])
```

!!! note "404, not 403"
    Env-gated routes return 404 with no response body to avoid revealing that the path exists at all.

---

### `get_state`

```python
async def get_state(path: str) -> RouteState
```

Retrieve the current state of a route.

```python title="example"
state = await engine.get_state("GET:/payments")
print(state.status, state.reason)
```

Raises `KeyError` if the path has not been registered. Read more in [RouteState](models.md#routestate).

---

### `list_states`

```python
async def list_states() -> list[RouteState]
```

Return all registered route states. Used by the CLI's `shield status` command and the admin dashboard.

```python title="example"
states = await engine.list_states()
for s in states:
    print(s.path, s.status)
```

---

## Scheduled maintenance

### `schedule_maintenance`

```python
async def schedule_maintenance(path: str, window: MaintenanceWindow) -> None
```

Schedule a future maintenance window without activating maintenance immediately. Creates an `asyncio.Task` that activates at `window.start` and restores `ACTIVE` at `window.end`.

```python title="example"
await engine.schedule_maintenance("GET:/payments", window=window)
```

Scheduled windows survive restarts: they are persisted to the backend and restored when the engine starts up.

---

### `cancel_schedule`

```python
async def cancel_schedule(path: str) -> None
```

Cancel any pending scheduled window for the given path. No-op if no window is scheduled.

---

## Global maintenance

Global maintenance blocks every route at once without requiring individual route changes. It is designed for emergency deployments, full-system downtime, or planned platform migrations.

### `enable_global_maintenance`

```python
async def enable_global_maintenance(
    reason: str = "",
    exempt_paths: list[str] | None = None,
    include_force_active: bool = False,
    actor: str = "system",
) -> None
```

Block all routes immediately. Exempt paths bypass the global check and respond normally.

??? example "Full example with exempt paths"

    ```python
    await engine.enable_global_maintenance(
        reason="Emergency patch, back in 15 minutes",
        exempt_paths=["/health", "GET:/admin/status"],
        include_force_active=False,
    )
    ```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | `str` | `""` | Shown in every 503 response while global maintenance is active |
| `exempt_paths` | `list[str] \| None` | `None` | Paths that bypass the global block. Use bare `/health` (all methods) or `GET:/health` (specific method). |
| `include_force_active` | `bool` | `False` | When `True`, even `@force_active` routes are blocked. Use only for hard lockdowns. |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |

!!! warning "Blocking `@force_active` routes"
    Setting `include_force_active=True` will block health check and readiness probe endpoints. Make sure load balancers and orchestrators can tolerate this before enabling it.

---

### `disable_global_maintenance`

```python
async def disable_global_maintenance(actor: str = "system") -> None
```

Restore all routes to their individual states. Each route resumes the status it had before global maintenance was enabled.

---

### `get_global_maintenance`

```python
async def get_global_maintenance() -> GlobalMaintenanceConfig
```

Return the current global maintenance configuration. Read more in [GlobalMaintenanceConfig](models.md#globalmaintenanceconfig).

```python title="example"
cfg = await engine.get_global_maintenance()
if cfg.enabled:
    print(f"Global maintenance ON: {cfg.reason}")
```

---

### `set_global_exempt_paths`

```python
async def set_global_exempt_paths(paths: list[str], actor: str = "system") -> None
```

Update the exempt path list while global maintenance is active, without toggling the mode on or off. Useful for adding emergency access to a monitoring endpoint mid-incident.

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

```python title="example"
# Last 50 entries across all routes
entries = await engine.get_audit_log(limit=50)

# All entries for a specific route
entries = await engine.get_audit_log(path="GET:/payments", limit=20)

for e in entries:
    print(e.timestamp, e.actor, e.action, e.previous_status, "→", e.new_status)
```

Read more in [AuditEntry](models.md#auditentry).

---

## Webhooks

## Rate limiting

### `set_rate_limit_policy`

```python
async def set_rate_limit_policy(
    path: str,
    method: str,
    limit: str,
    algorithm: str = "fixed_window",
    key_strategy: str = "ip",
    burst: int = 0,
    actor: str = "system",
) -> RateLimitPolicy
```

Register or update a rate limit policy at runtime. Persists to the backend so restarts and other instances pick it up. Logged in the audit log as `rl_policy_set` (new) or `rl_policy_updated` (existing policy replaced).

```python title="example"
await engine.set_rate_limit_policy(
    "/public/posts", "GET", "20/minute", actor="alice"
)
```

---

### `delete_rate_limit_policy`

```python
async def delete_rate_limit_policy(path: str, method: str, actor: str = "system") -> None
```

Remove a persisted rate limit policy override. Logged as `rl_policy_deleted`.

```python title="example"
await engine.delete_rate_limit_policy("/public/posts", "GET", actor="alice")
```

---

### `reset_rate_limit`

```python
async def reset_rate_limit(path: str, method: str | None = None, actor: str = "system") -> None
```

Clear rate limit counters for a route immediately. When `method` is `None`, counters for all methods on the path are cleared. Logged as `rl_reset`.

```python title="example"
await engine.reset_rate_limit("/public/posts", "GET", actor="alice")
await engine.reset_rate_limit("/public/posts")   # all methods
```

---

### `get_rate_limit_hits`

```python
async def get_rate_limit_hits(path: str | None = None, limit: int = 100) -> list[RateLimitHit]
```

Return blocked request records, newest first.

```python title="example"
hits = await engine.get_rate_limit_hits(limit=50)
hits = await engine.get_rate_limit_hits(path="/public/posts")
```

---

### `list_rate_limit_policies`

```python
async def list_rate_limit_policies() -> list[RateLimitPolicy]
```

Return all registered rate limit policies.

---

## Webhooks

### `add_webhook`

```python
def add_webhook(url: str, formatter=None) -> None
```

Register a URL to receive HTTP POST notifications on every state change.

```python title="generic JSON webhook"
engine.add_webhook("https://my-service.example.com/shield-events")
```

```python title="Slack webhook"
from shield.core.webhooks import SlackWebhookFormatter

engine.add_webhook(
    "https://hooks.slack.com/services/...",
    formatter=SlackWebhookFormatter(),
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | required | The HTTP endpoint that receives the POST |
| `formatter` | `callable \| None` | `None` | A callable that returns the POST payload. `None` uses the default JSON formatter. Pass `SlackWebhookFormatter()` for Slack-compatible blocks. |

??? info "Default JSON payload"

    ```json
    {
      "event": "maintenance_on",
      "path": "GET:/payments",
      "reason": "DB migration",
      "timestamp": "2025-06-01T02:00:00Z",
      "state": { "path": "GET:/payments", "status": "maintenance" }
    }
    ```

!!! note "Webhook failures are silent"
    Webhook delivery runs in a background task. Errors are logged and never propagated to the request path or the caller.
