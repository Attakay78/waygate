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

## Sync API — `engine.sync`

Every async engine method has a synchronous mirror available via `engine.sync`. This is useful for **sync FastAPI route handlers** (plain `def`, not `async def`) and background threads, where you cannot use `await`.

FastAPI automatically runs sync handlers in a worker thread, which is exactly the context `engine.sync` requires. No additional setup is needed — just use `engine.sync.*` instead of `await engine.*`.

!!! warning "Do not call from inside `async def`"
    `engine.sync.*` uses `anyio.from_thread.run()` internally. Calling it from inside an async function (on the event loop thread) will deadlock. Use `await engine.*` there instead.

=== "Async handler (normal)"

    ```python
    @router.post("/admin/deploy")
    @force_active
    async def deploy():
        await engine.disable("GET:/payments", reason="deploy in progress")
        await run_migration()
        await engine.enable("GET:/payments")
        return {"deployed": True}
    ```

=== "Sync handler (engine.sync)"

    ```python
    @router.post("/admin/deploy")
    @force_active
    def deploy():  # FastAPI runs this in a worker thread automatically
        engine.sync.disable("GET:/payments", reason="deploy in progress")
        run_migration()
        engine.sync.enable("GET:/payments")
        return {"deployed": True}
    ```

=== "Background thread"

    ```python
    import threading

    def nightly_job():
        engine.sync.set_maintenance("GET:/reports", reason="nightly rebuild")
        rebuild_reports()
        engine.sync.enable("GET:/reports")

    threading.Thread(target=nightly_job, daemon=True).start()
    ```

### Available methods

`engine.sync` exposes the same public API as the async engine:

| `engine.sync.*` | Async equivalent |
|---|---|
| `enable(path, actor, reason)` | `await engine.enable(...)` |
| `disable(path, reason, actor)` | `await engine.disable(...)` |
| `set_maintenance(path, reason, window, actor)` | `await engine.set_maintenance(...)` |
| `schedule_maintenance(path, window, actor)` | `await engine.schedule_maintenance(...)` |
| `set_env_only(path, envs, actor)` | `await engine.set_env_only(...)` |
| `get_global_maintenance()` | `await engine.get_global_maintenance()` |
| `enable_global_maintenance(reason, ...)` | `await engine.enable_global_maintenance(...)` |
| `disable_global_maintenance(actor)` | `await engine.disable_global_maintenance(...)` |
| `set_global_exempt_paths(paths)` | `await engine.set_global_exempt_paths(...)` |
| `get_rate_limit_hits(path, limit)` | `await engine.get_rate_limit_hits(...)` |
| `set_rate_limit_policy(path, method, limit, ...)` | `await engine.set_rate_limit_policy(...)` |
| `delete_rate_limit_policy(path, method, actor)` | `await engine.delete_rate_limit_policy(...)` |
| `reset_rate_limit(path, method, actor)` | `await engine.reset_rate_limit(...)` |
| `set_global_rate_limit(limit, ...)` | `await engine.set_global_rate_limit(...)` |
| `get_global_rate_limit()` | `await engine.get_global_rate_limit()` |
| `delete_global_rate_limit(actor)` | `await engine.delete_global_rate_limit(...)` |
| `reset_global_rate_limit(actor)` | `await engine.reset_global_rate_limit(...)` |
| `enable_global_rate_limit(actor)` | `await engine.enable_global_rate_limit(...)` |
| `disable_global_rate_limit(actor)` | `await engine.disable_global_rate_limit(...)` |
| `get_state(path)` | `await engine.get_state(path)` |
| `list_states()` | `await engine.list_states()` |
| `get_audit_log(path, limit)` | `await engine.get_audit_log(...)` |

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

## Per-service maintenance

Per-service maintenance puts **all routes of one service** into maintenance mode at once, without touching other services or requiring individual route changes. It uses the same `GlobalMaintenanceConfig` model and exempt-paths mechanism as all-services global maintenance, but is scoped to a single `app_id`.

Available via the engine, the `shield sm` CLI command group, the REST API (`POST /api/services/{service}/maintenance/enable|disable`), and the dashboard Routes page when a service filter is active.

Audit log actions: `service_maintenance_on` (enabled), `service_maintenance_off` (disabled). The `Path` column displays as `[{service} Maintenance]`.

### `enable_service_maintenance`

```python
async def enable_service_maintenance(
    service: str,
    reason: str = "",
    exempt_paths: list[str] | None = None,
    include_force_active: bool = False,
    actor: str = "system",
    platform: str = "system",
) -> GlobalMaintenanceConfig
```

Block all routes for *service* immediately. SDK clients with a matching `app_id` receive the sentinel via SSE and treat every request as if their own global maintenance were enabled. Exempt paths respond normally.

```python title="example"
await engine.enable_service_maintenance(
    "payments-service",
    reason="DB migration",
    exempt_paths=["/health"],
    actor="alice",
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `service` | `str` | required | The service `app_id` to put into maintenance |
| `reason` | `str` | `""` | Shown in every 503 response while maintenance is active |
| `exempt_paths` | `list[str] \| None` | `None` | Paths that bypass the service maintenance check. Bare path or `METHOD:/path`. |
| `include_force_active` | `bool` | `False` | When `True`, even `@force_active` routes on this service are blocked |
| `actor` | `str` | `"system"` | Identity recorded in the audit log |

---

### `disable_service_maintenance`

```python
async def disable_service_maintenance(
    service: str,
    actor: str = "system",
    platform: str = "system",
) -> GlobalMaintenanceConfig
```

Restore all routes of *service* to their individual states.

```python title="example"
await engine.disable_service_maintenance("payments-service", actor="alice")
```

---

### `get_service_maintenance`

```python
async def get_service_maintenance(service: str) -> GlobalMaintenanceConfig
```

Return the current per-service maintenance configuration. Returns a `GlobalMaintenanceConfig` with `enabled=False` when no sentinel is stored for this service.

```python title="example"
cfg = await engine.get_service_maintenance("payments-service")
if cfg.enabled:
    print(f"payments-service in maintenance: {cfg.reason}")
```

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

## Global rate limit

A single policy applied across all routes with higher precedence than per-route limits. Checked first on every request — a request blocked by the global limit never touches the per-route counter. Per-route checks only run after the global limit passes (or the route is exempt, or no global limit is configured).

### `set_global_rate_limit`

```python
async def set_global_rate_limit(
    limit: str,
    *,
    algorithm: str | None = None,
    key_strategy: str | None = None,
    on_missing_key: str | None = None,
    burst: int = 0,
    exempt_routes: list[str] | None = None,
    actor: str = "system",
    platform: str = "",
) -> GlobalRateLimitPolicy
```

Create or replace the global rate limit policy. Logged as `global_rl_set` or `global_rl_updated`.

---

### `get_global_rate_limit`

```python
async def get_global_rate_limit() -> GlobalRateLimitPolicy | None
```

Return the current policy, or `None` if not configured.

---

### `delete_global_rate_limit`

```python
async def delete_global_rate_limit(*, actor: str = "system") -> None
```

Remove the global policy. Logged as `global_rl_deleted`.

---

### `reset_global_rate_limit`

```python
async def reset_global_rate_limit(*, actor: str = "system") -> None
```

Clear all global counters. Policy is kept. Logged as `global_rl_reset`.

---

### `enable_global_rate_limit`

```python
async def enable_global_rate_limit(*, actor: str = "system") -> None
```

Resume a paused global policy. No-op if already enabled. Logged as `global_rl_enabled`.

---

### `disable_global_rate_limit`

```python
async def disable_global_rate_limit(*, actor: str = "system") -> None
```

Pause the global policy without removing it. Per-route policies are unaffected. Logged as `global_rl_disabled`.

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
