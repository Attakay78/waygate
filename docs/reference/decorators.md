# Decorators

Decorators are the primary way to declare route lifecycle state. They stamp `__shield_meta__` on the endpoint function — the function itself is never modified. `ShieldRouter` reads this metadata at startup and registers the initial state with the engine.

All decorators are imported from `shield.fastapi.decorators` (or directly from `shield.fastapi`).

---

## `@maintenance`

Put a route in maintenance mode. Returns **503** with a structured JSON body.

```python
from shield.fastapi.decorators import maintenance

@router.get("/payments")
@maintenance(reason="DB migration in progress")
async def get_payments():
    return {"payments": []}
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | `str` | `""` | Human-readable reason shown in the error response |
| `start` | `datetime \| None` | `None` | Start of maintenance window (UTC) |
| `end` | `datetime \| None` | `None` | End of maintenance window (UTC) — sets `Retry-After` |

### With a scheduled window

```python
from datetime import datetime, UTC

@router.post("/orders")
@maintenance(
    reason="Order system upgrade",
    start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
    end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
)
async def create_order():
    ...
```

The scheduler will automatically activate maintenance at `start` and restore `ACTIVE` at `end`.

### Response

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

Permanently disable a route. Returns **503**. Use for routes that should never be called again (removed features, old API versions).

```python
from shield.fastapi.decorators import disabled

@router.get("/legacy/report")
@disabled(reason="Replaced by /v2/reports — update your clients")
async def legacy_report():
    ...
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `reason` | `str` | `""` | Shown in the error response and audit log |

### Response

```json
{
  "error": {
    "code": "ROUTE_DISABLED",
    "message": "This endpoint has been disabled",
    "reason": "Replaced by /v2/reports — update your clients",
    "path": "GET:/legacy/report",
    "retry_after": null
  }
}
```

---

## `@env_only`

Restrict a route to specific environment names. In any other environment the route returns a **silent 404** — it does not reveal the path exists. Use for internal tools, debug endpoints, or staging-only features.

```python
from shield.fastapi.decorators import env_only

@router.get("/internal/metrics")
@env_only("dev", "staging")
async def internal_metrics():
    ...
```

### Parameters

Accepts one or more positional `str` arguments — the environment names where the route is allowed.

```python
@env_only("dev")              # single env
@env_only("dev", "staging")   # multiple envs
```

### Setting the current environment

```python
engine = ShieldEngine(current_env="production")
# or
engine = make_engine(current_env="staging")
# or via env var:
# SHIELD_ENV=production
```

### Response (wrong environment)

Returns `404` with **no response body** — intentionally silent to avoid leaking that the path exists.

---

## `@force_active`

Bypass all shield checks. Use for health checks, readiness probes, and any route that must always be reachable regardless of maintenance state.

```python
from shield.fastapi.decorators import force_active

@router.get("/health")
@force_active
async def health():
    return {"status": "ok"}
```

### Notes

- `@force_active` takes no arguments (it is used without parentheses).
- Routes marked `@force_active` **cannot** be disabled or put in maintenance via the engine or CLI — this is intentional. Health check routes must be trustworthy.
- The only exception is when global maintenance is enabled with `include_force_active=True`.

---

## `@deprecated`

Mark a route as deprecated. Requests still succeed, but the middleware injects RFC-compliant response headers to warn clients.

```python
from shield.fastapi.decorators import deprecated

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
| `sunset` | `str` | required | RFC 7231 date string when the route will be removed |
| `use_instead` | `str` | `""` | Path or URL of the successor resource |

### Response headers injected automatically

```http
Deprecation: true
Sunset: Sat, 01 Jan 2027 00:00:00 GMT
Link: </v2/users>; rel="successor-version"
```

The route is also marked `deprecated: true` in the OpenAPI schema.

---

## Custom responses

By default, blocked routes return a structured JSON error body. You can replace this with any response — HTML, plain text, a redirect, or your own JSON shape — in two ways:

- **Per-route**: pass `response=` directly on the decorator
- **Global default**: pass `responses=` on `ShieldMiddleware`

Resolution order per request: **per-route** → **global default** → **built-in JSON**.

---

### Per-route: `response=` parameter

Every blocking decorator (`@maintenance`, `@disabled`, `@env_only`) accepts an optional `response=` keyword argument — a sync or async callable with the signature:

```python
(request: Request, exc: Exception) -> Response
```

**HTML maintenance page**

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse
from shield.fastapi import maintenance

def maintenance_page(request: Request, exc: Exception) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Down for maintenance</h1><p>{exc.reason}</p>",
        status_code=503,
    )

@router.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC", response=maintenance_page)
async def payments():
    return {"payments": []}
```

**Inline lambda for simple cases**

```python
from starlette.responses import RedirectResponse

@router.get("/payments")
@maintenance(reason="DB migration", response=lambda *_: RedirectResponse("/status"))
async def payments():
    return {"payments": []}
```

**Async factory (template rendering, DB lookup, etc.)**

```python
async def maintenance_page(request: Request, exc: Exception) -> HTMLResponse:
    html = await render_template("maintenance.html", reason=exc.reason)
    return HTMLResponse(html, status_code=503)

@router.get("/payments")
@maintenance(reason="DB migration", response=maintenance_page)
async def payments():
    return {"payments": []}
```

**Custom JSON structure**

```python
from starlette.responses import JSONResponse

def branded_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "message": str(exc), "support": "https://status.example.com"},
        status_code=503,
    )

@router.get("/payments")
@maintenance(reason="DB migration", response=branded_error)
async def payments():
    return {"payments": []}
```

**Reusable factory on multiple routes**

```python
def maintenance_html(request: Request, exc: Exception) -> HTMLResponse:
    return HTMLResponse("<h1>Temporarily unavailable</h1>", status_code=503)

@router.get("/payments")
@maintenance(reason="DB migration", response=maintenance_html)
async def payments(): ...

@router.post("/orders")
@maintenance(reason="DB migration", response=maintenance_html)
async def create_order(): ...
```

---

### Global default: `responses=` on `ShieldMiddleware`

Set app-wide response defaults once on the middleware. Any route without a per-route `response=` will use these. Per-route always wins.

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse
from shield.fastapi import ShieldMiddleware

def maintenance_page(request: Request, exc: Exception) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Down for maintenance</h1><p>{exc.reason}</p>",
        status_code=503,
    )

app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "maintenance": maintenance_page,
        "disabled": lambda req, exc: HTMLResponse(
            f"<h1>Gone</h1><p>{exc.reason}</p>", status_code=503
        ),
        # "env_gated": ...  # omit to keep the default silent 404
    },
)
```

Supported keys:

| Key | Triggered by | Default behaviour |
|---|---|---|
| `"maintenance"` | `MaintenanceException` (route or global) | 503 JSON |
| `"disabled"` | `RouteDisabledException` | 503 JSON |
| `"env_gated"` | `EnvGatedException` | Silent 404 |

---

### Factory signature reference

```python
# Sync
def my_factory(request: Request, exc: Exception) -> Response: ...

# Async — works identically
async def my_factory(request: Request, exc: Exception) -> Response: ...
```

The `exc` argument is the `ShieldException` subclass that triggered the block:

| Shield state | Exception type | Useful attributes |
|---|---|---|
| Maintenance | `MaintenanceException` | `exc.reason`, `exc.retry_after` |
| Disabled | `RouteDisabledException` | `exc.reason` |
| Env-gated | `EnvGatedException` | `exc.path`, `exc.current_env`, `exc.allowed_envs` |

Any Starlette `Response` subclass is valid: `HTMLResponse`, `JSONResponse`, `RedirectResponse`, `PlainTextResponse`, or a raw `Response`.

---

## Decorator composition rules

- Always apply the shield decorator **directly below** the router decorator:

    ```python
    @router.get("/payments")   # ← router decorator (outermost)
    @maintenance(reason="...")  # ← shield decorator (innermost)
    async def get_payments():
        ...
    ```

- All decorators are compatible with both `ShieldRouter` and plain `APIRouter`.
- Multiple shield decorators on the same function are not supported — the last one to stamp `__shield_meta__` wins.

---

## Using decorators as `Depends()` dependencies

All decorators also work as FastAPI dependencies for per-handler enforcement without middleware:

```python
@router.get("/admin/report", dependencies=[Depends(disabled(reason="Use /v2/report"))])
async def admin_report():
    return {}
```

See [**FastAPI adapter: Dependency injection**](../adapters/fastapi.md#dependency-injection) for details.
