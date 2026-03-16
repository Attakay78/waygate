# Decorators

Decorators are the primary way to declare the lifecycle state of a route. Each one stamps a `__shield_meta__` dictionary onto the endpoint function without modifying it. `ShieldRouter` reads this metadata at startup and registers the initial state with the engine.

All decorators are importable from `shield.fastapi` or directly from `shield.fastapi.decorators`.

```python
from shield.fastapi import maintenance, disabled, env_only, force_active, deprecated
from shield.fastapi.decorators import rate_limit
```

!!! tip "Decorator order"
    Always apply the shield decorator **directly below** the router decorator, so it wraps the function before the router sees it:

    ```python
    @router.get("/payments")   # outermost
    @maintenance(reason="...") # innermost — wraps the function
    async def get_payments():
        ...
    ```

---

## `@maintenance`

Mark a route as temporarily unavailable. Returns **503** with a structured JSON body and an optional `Retry-After` header.

```python title="basic usage"
from shield.fastapi import maintenance

@router.get("/payments")
@maintenance(reason="DB migration in progress")
async def get_payments():
    return {"payments": []}
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | `str` | `""` | Human-readable explanation shown in the 503 error response and recorded in the audit log |
| `start` | `datetime \| None` | `None` | When maintenance should activate. If omitted, the route enters maintenance immediately on startup. |
| `end` | `datetime \| None` | `None` | When maintenance should deactivate. Sets the `Retry-After` response header. Read more in [MaintenanceWindow](models.md#maintenancewindow). |
| `response` | `callable \| None` | `None` | Custom response factory for this route. Read more in [Custom responses](#custom-responses). |

### Scheduled window

Pass `start` and `end` to schedule maintenance for a future window. The scheduler activates maintenance at `start` and restores `ACTIVE` at `end` automatically.

```python title="scheduled maintenance"
from datetime import datetime, UTC
from shield.fastapi import maintenance

@router.post("/orders")
@maintenance(
    reason="Order system upgrade",
    start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
    end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
)
async def create_order():
    ...
```

### Response body

```json
{
  "error": {
    "code": "MAINTENANCE_MODE",
    "message": "This endpoint is temporarily unavailable",
    "reason": "DB migration in progress",
    "path": "GET:/payments",
    "retry_after": "2025-06-01T04:00:00Z"
  }
}
```

---

## `@disabled`

Permanently disable a route. Returns **503**. Use for routes that should never be called again — removed features, deprecated API versions that have passed sunset, or endpoints replaced by a successor path.

```python title="basic usage"
from shield.fastapi import disabled

@router.get("/legacy/report")
@disabled(reason="Replaced by /v2/reports. Update your client.")
async def legacy_report():
    ...
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | `str` | `""` | Shown in the 503 error response and recorded in the audit log |
| `response` | `callable \| None` | `None` | Custom response factory for this route. Read more in [Custom responses](#custom-responses). |

### Response body

```json
{
  "error": {
    "code": "ROUTE_DISABLED",
    "message": "This endpoint has been disabled",
    "reason": "Replaced by /v2/reports. Update your client.",
    "path": "GET:/legacy/report",
    "retry_after": null
  }
}
```

!!! tip "Prefer `@disabled` over deleting the route"
    Removing a route entirely causes clients to receive unhandled 404s with no explanation. `@disabled` returns a 503 with a machine-readable error code and a human-readable reason, making it easier for API consumers to diagnose and migrate.

---

## `@env_only`

Restrict a route to specific environment names. In any other environment the route returns a **silent 404** — no response body, no indication that the path exists.

Use this for internal tools, debug endpoints, admin utilities, or staging-only features that should never be accessible in production.

```python title="basic usage"
from shield.fastapi import env_only

@router.get("/internal/metrics")
@env_only("dev", "staging")
async def internal_metrics():
    ...
```

### Parameters

`@env_only` accepts one or more positional string arguments — the environment names where the route is accessible.

```python
@env_only("dev")                   # single environment
@env_only("dev", "staging")        # multiple environments
@env_only("dev", "staging", "qa")  # three environments
```

### Setting the current environment

The engine compares the environment names against the `current_env` value it was constructed with:

```python title="setting the environment"
# Explicit
engine = ShieldEngine(current_env="production")

# Via config helper (reads SHIELD_ENV env var)
engine = make_engine()
```

```ini title=".shield file"
SHIELD_ENV=staging
```

!!! note "404, not 403"
    Env-gated routes return 404 with **no body** to avoid leaking that the path exists in other environments. A 403 would reveal the route is present but forbidden.

---

## `@force_active`

Bypass all shield checks — both per-route and global maintenance. Use for health check endpoints, readiness probes, and any path that must remain reachable regardless of system state.

```python title="basic usage"
from shield.fastapi import force_active

@router.get("/health")
@force_active
async def health():
    return {"status": "ok"}
```

!!! note "`@force_active` takes no arguments"
    Use it without parentheses, directly on the function.

### Behavior

- Routes marked `@force_active` **cannot** be disabled or put in maintenance via the engine, CLI, or admin dashboard.
- Global maintenance skips these routes by default. They are only blocked when `enable_global_maintenance(include_force_active=True)` is explicitly passed.

!!! tip "Always mark health checks `@force_active`"
    Load balancers and orchestrators rely on health endpoints to determine if a pod should receive traffic. If your health route is blocked during a maintenance window, the orchestrator may restart or decommission the instance.

---

## `@deprecated`

Mark a route as deprecated. Requests still succeed (no blocking), but the middleware injects RFC-compliant headers into every response to warn API consumers and tooling.

```python title="basic usage"
from shield.fastapi import deprecated

@router.get("/v1/users")
@deprecated(
    sunset="Sat, 01 Jan 2027 00:00:00 GMT",
    use_instead="/v2/users",
)
async def v1_users():
    return {"users": []}
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sunset` | `str` | required | RFC 7231 date string indicating when the route will be removed. Shown in the `Sunset` response header. |
| `use_instead` | `str` | `""` | Path or URL of the successor resource. Shown in the `Link` response header. |

### Response headers injected

```http
Deprecation: true
Sunset: Sat, 01 Jan 2027 00:00:00 GMT
Link: </v2/users>; rel="successor-version"
```

The route is also automatically marked `deprecated: true` in the OpenAPI schema, so clients and generated SDKs pick up the deprecation without any manual annotation.

!!! tip "Use `@deprecated` before `@disabled`"
    Give API consumers time to migrate. Mark the route `@deprecated` with a sunset date, then switch to `@disabled` after the sunset date passes.

---

## Custom responses

By default, blocked routes return a structured JSON error body. You can replace this with any Starlette `Response` subclass — HTML, plain text, a redirect, or a different JSON shape — in two ways:

- **Per-route**: pass `response=` on the decorator
- **Global default**: pass `responses=` on `ShieldMiddleware`

**Resolution order per request:** per-route `response=` → global `responses=` default → built-in JSON.

---

### Per-route: `response=` parameter

Every blocking decorator (`@maintenance`, `@disabled`, `@env_only`) accepts an optional `response=` keyword. The value is a sync or async callable:

```python
(request: Request, exc: ShieldException) -> Response
```

??? example "HTML maintenance page"

    ```python
    from starlette.requests import Request
    from starlette.responses import HTMLResponse
    from shield.fastapi import maintenance

    def maintenance_page(request: Request, exc) -> HTMLResponse:
        return HTMLResponse(
            f"<h1>Down for maintenance</h1><p>{exc.reason}</p>",
            status_code=503,
        )

    @router.get("/payments")
    @maintenance(reason="DB migration", response=maintenance_page)
    async def payments():
        return {"payments": []}
    ```

??? example "Redirect to a status page"

    ```python
    from starlette.responses import RedirectResponse
    from shield.fastapi import maintenance

    @router.get("/payments")
    @maintenance(
        reason="DB migration",
        response=lambda *_: RedirectResponse("/status"),
    )
    async def payments():
        return {"payments": []}
    ```

??? example "Custom JSON shape"

    ```python
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from shield.fastapi import maintenance

    def branded_error(request: Request, exc) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "message": str(exc), "support": "https://status.example.com"},
            status_code=503,
        )

    @router.get("/payments")
    @maintenance(reason="DB migration", response=branded_error)
    async def payments():
        return {"payments": []}
    ```

??? example "Async factory (template rendering)"

    ```python
    from starlette.requests import Request
    from starlette.responses import HTMLResponse
    from shield.fastapi import maintenance

    async def maintenance_page(request: Request, exc) -> HTMLResponse:
        html = await render_template("maintenance.html", reason=exc.reason)
        return HTMLResponse(html, status_code=503)

    @router.get("/payments")
    @maintenance(reason="DB migration", response=maintenance_page)
    async def payments():
        return {"payments": []}
    ```

---

### Global default: `responses=` on `ShieldMiddleware`

Set app-wide response defaults once on the middleware. Any route without a per-route `response=` will use these.

```python title="app-wide custom responses"
from starlette.requests import Request
from starlette.responses import HTMLResponse
from shield.fastapi import ShieldMiddleware

def maintenance_page(request: Request, exc) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Down for maintenance</h1><p>{exc.reason}</p>",
        status_code=503,
    )

app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "maintenance": maintenance_page,
        "disabled": lambda req, exc: HTMLResponse("<h1>Gone</h1>", status_code=503),
        # omit "env_gated" to keep the default silent 404
    },
)
```

| Key | Triggered by | Default behavior |
|---|---|---|
| `"maintenance"` | `MaintenanceException` (per-route or global) | 503 JSON |
| `"disabled"` | `RouteDisabledException` | 503 JSON |
| `"env_gated"` | `EnvGatedException` | Silent 404 |
| `"rate_limited"` | `RateLimitExceededException` | 429 JSON |

---

### Factory signature

```python
# Sync — works fine for most cases
def my_factory(request: Request, exc: ShieldException) -> Response: ...

# Async — identical interface, use when you need to await something
async def my_factory(request: Request, exc: ShieldException) -> Response: ...
```

The `exc` argument carries useful context for building your response:

| Shield state | Exception type | Useful attributes |
|---|---|---|
| Maintenance | `MaintenanceException` | `exc.reason`, `exc.retry_after`, `exc.path` |
| Disabled | `RouteDisabledException` | `exc.reason`, `exc.path` |
| Env-gated | `EnvGatedException` | `exc.path`, `exc.current_env`, `exc.allowed_envs` |
| Rate limited | `RateLimitExceededException` | `exc.limit`, `exc.retry_after_seconds`, `exc.reset_at`, `exc.remaining`, `exc.key` |

Read more in [Exceptions](exceptions.md).

---

## `@rate_limit`

Cap the number of requests a client can make in a given time window. Returns **429** with `Retry-After` and `X-RateLimit-*` headers when the limit is exceeded.

```python
from shield.fastapi.decorators import rate_limit

@router.get("/public/posts")
@rate_limit("10/minute")
async def list_posts():
    return {"posts": [...]}
```

### Basic parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | `str \| dict` | required | `"100/minute"` or a tier dict `{"free": "10/minute", "pro": "100/minute"}` |
| `algorithm` | `str` | `"fixed_window"` | `fixed_window`, `sliding_window`, `moving_window`, or `token_bucket` |
| `key` | `str \| callable` | `"ip"` | `"ip"`, `"user"`, `"api_key"`, `"global"`, or an async callable |
| `on_missing_key` | `str \| None` | strategy default | `"exempt"`, `"fallback_ip"`, or `"block"` |
| `burst` | `int` | `0` | Extra requests above the base limit |
| `exempt_ips` | `list[str] \| None` | `[]` | CIDR ranges that bypass the limit |
| `exempt_roles` | `list[str] \| None` | `[]` | Roles that bypass the limit |

### Key strategies

```python
@rate_limit("100/minute")                          # per IP (default)
@rate_limit("100/minute", key="user")              # per request.state.user_id
@rate_limit("50/minute",  key="api_key")           # per X-API-Key header
@rate_limit("5/minute",   key="global")            # shared counter for all callers
@rate_limit("100/minute", key=my_async_fn)         # custom extractor
```

### Tiered limits

```python
@rate_limit(
    {"free": "10/minute", "pro": "100/minute", "enterprise": "unlimited"},
    key="user",
)
```

The tier is read from `request.state.plan` by default. Override with `tier_resolver="your_attr"`.

!!! tip "Requires installation"
    ```bash
    uv add "api-shield[rate-limit]"
    ```

See [**Tutorial: Rate Limiting**](../tutorial/rate-limiting.md) and [**Reference: Rate Limiting**](rate-limiting.md) for the full API.

---

## Using decorators as `Depends()` dependencies

All decorators also work as FastAPI dependencies. This lets you enforce route state without middleware — useful in testing or when you need per-handler enforcement in a router that does not use `ShieldRouter`.

```python title="dependency injection usage"
from fastapi import Depends
from shield.fastapi import disabled

@router.get("/admin/report", dependencies=[Depends(disabled(reason="Use /v2/report"))])
async def admin_report():
    return {}
```

See [FastAPI adapter: Dependency injection](../adapters/fastapi.md#dependency-injection) for full details.

---

## Composition rules

- A route can carry **at most one** shield decorator. If multiple are applied, the last one to write `__shield_meta__` wins. Use `@maintenance` or `@disabled`, not both.
- All decorators preserve `async` and `sync` function signatures using `@functools.wraps`.
- Decorators are compatible with both `ShieldRouter` and plain `APIRouter`. When using a plain `APIRouter`, the decorator metadata is applied, but initial state registration at startup requires `ShieldRouter`. Read more in [ShieldRouter](../adapters/fastapi.md#shieldrouter).
