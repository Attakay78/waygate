# Middleware & OpenAPI

This page covers `WaygateMiddleware`, which enforces route state on every HTTP request, and the two OpenAPI helpers that keep your `/docs` accurate at runtime.

---

## WaygateMiddleware

`WaygateMiddleware` is a standard ASGI middleware (Starlette `BaseHTTPMiddleware`). Add it once to your ASGI app and it automatically intercepts every request, calls `engine.check()`, and returns the appropriate error response when a route is blocked. It works with any Starlette-compatible ASGI framework, including FastAPI.

```python
from waygate.fastapi import WaygateMiddleware
```

### Setup

```python title="main.py"
from fastapi import FastAPI
from waygate.fastapi import WaygateMiddleware
from waygate import WaygateEngine

engine = WaygateEngine()
app = FastAPI()

app.add_middleware(WaygateMiddleware, engine=engine)
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `engine` | `WaygateEngine` | required | The engine instance to delegate all `check()` calls to. Read more in [WaygateEngine](engine.md). |
| `responses` | `dict \| None` | `None` | App-wide custom response factories, keyed by error type. Read more in [Custom responses](decorators.md#custom-responses). |

### Request flow

Every incoming request passes through this sequence:

```
Incoming HTTP request
        │
        ▼
WaygateMiddleware.dispatch()
        │
        ├─ Path is /docs, /redoc, or /openapi.json?  → pass through
        ├─ Path starts with /waygate/ (admin)?         → pass through
        │
        ├─ Lazy-scan routes for __waygate_meta__ (once, on first request)
        │
        ├─ Route has force_active=True?               → pass through
        │
        ▼
engine.check(path, method)
        │
        ├─ Global maintenance ON + path not exempt    → 503 JSON
        ├─ MAINTENANCE                                → 503 JSON + Retry-After
        ├─ DISABLED                                   → 503 JSON
        ├─ ENV_GATED (wrong environment)              → 403 JSON
        ├─ DEPRECATED                                 → call_next + inject headers
        └─ ACTIVE                                     → call_next ✓
```

### Error responses

All error responses use a consistent JSON structure:

```json
{
  "error": {
    "code": "MAINTENANCE_MODE",
    "message": "This endpoint is temporarily unavailable",
    "reason": "Database migration in progress",
    "path": "GET:/payments",
    "retry_after": "2025-06-01T04:00:00Z"
  }
}
```

| Scenario | HTTP status | `code` field |
|---|---|---|
| Route in maintenance | 503 | `MAINTENANCE_MODE` |
| Route disabled | 503 | `ROUTE_DISABLED` |
| Global maintenance active | 503 | `MAINTENANCE_MODE` |
| Env-gated (wrong environment) | 403 | `ENV_GATED` |

### Deprecation headers

For routes with status `DEPRECATED`, the middleware injects RFC-compliant headers without blocking the request. The response still reaches the client with a 200:

```http
Deprecation: true
Sunset: Sat, 01 Jan 2027 00:00:00 GMT
Link: </v2/users>; rel="successor-version"
```

!!! note "Custom responses"
    You can replace any of the default JSON error responses with HTML, redirects, or a different JSON shape — either per-route or globally on the middleware. Read more in [Custom responses](decorators.md#custom-responses).

---

## `apply_waygate_to_openapi` (FastAPI only)

Keep your FastAPI OpenAPI schema accurate by filtering it based on the current route states at runtime. Disabled and env-gated routes are hidden. Maintenance routes are annotated. Deprecated routes are flagged.

```python
from waygate.fastapi import apply_waygate_to_openapi
```

### Setup

```python title="main.py"
from waygate.fastapi import apply_waygate_to_openapi

apply_waygate_to_openapi(app, engine)
```

!!! warning "Call after `app.include_router()`"
    `apply_waygate_to_openapi` patches `app.openapi()`. Call it after all routers have been included so the full route list is available when the patch is applied.

### Parameters

| Parameter | Type | Description |
|---|---|---|
| `app` | `FastAPI` | The FastAPI application instance to patch |
| `engine` | `WaygateEngine` | The engine whose current state is used to filter the schema |

### Schema behavior by route status

| Route status | OpenAPI schema behavior |
|---|---|
| `ACTIVE` | No change |
| `MAINTENANCE` | Summary prefixed with `🔧`; description block shows a warning; `x-waygate-status` extension added |
| `DISABLED` | Hidden from all schemas — not visible in `/docs`, `/redoc`, or `/openapi.json` |
| `ENV_GATED` (wrong environment) | Hidden from all schemas |
| `DEPRECATED` | Marked `deprecated: true`; successor path shown in description |

The schema is re-computed on every request to `/openapi.json`, so runtime state changes reflect immediately without a restart.

---

## `setup_waygate_docs` (FastAPI only)

Enhance FastAPI's `/docs` and `/redoc` with live status indicators that update automatically as route states change.

```python
from waygate.fastapi import apply_waygate_to_openapi, setup_waygate_docs
```

### Setup

```python title="main.py"
# apply_waygate_to_openapi must be called first
apply_waygate_to_openapi(app, engine)
setup_waygate_docs(app, engine)
```

### Parameters

| Parameter | Type | Description |
|---|---|---|
| `app` | `FastAPI` | The FastAPI application instance |
| `engine` | `WaygateEngine` | The engine whose state drives the UI indicators |

### What it adds to `/docs`

| Condition | UI indicator |
|---|---|
| Global maintenance **ON** | Full-width pulsing red banner with the reason text and exempt paths; auto-refreshes every 15 seconds |
| Global maintenance **OFF** | Small green "All systems operational" chip in the bottom-right corner |
| Per-route maintenance | Orange left-border on the operation block with a `🔧 MAINTENANCE` badge |

!!! tip "Use both together"
    `apply_waygate_to_openapi` keeps the schema accurate (hiding disabled routes, marking deprecated ones). `setup_waygate_docs` adds the live status UI on top. They are independent — use one, both, or neither.
