# Adding middleware

`ShieldMiddleware` is the enforcement layer. It is a standard ASGI middleware that intercepts every HTTP request, calls `engine.check()`, and returns the appropriate error response when a route is blocked. Without it, decorators register state but nothing enforces it.

The examples below use **FastAPI**, but `ShieldMiddleware` works with any [Starlette](https://www.starlette.io/)-compatible ASGI framework.

---

## Basic setup

```python title="app.py"
from fastapi import FastAPI
from shield.core.engine import ShieldEngine
from shield.fastapi.middleware import ShieldMiddleware

engine = ShieldEngine()  # uses MemoryBackend by default

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)
```

!!! important
    Add `ShieldMiddleware` **before** including any routers. Middleware is applied in reverse registration order in ASGI frameworks built on Starlette, so adding it first ensures it wraps all routes.

---

## What the middleware does

```
Incoming HTTP request
        │
        ▼
ShieldMiddleware.dispatch()
        │
        ├─ /docs, /redoc, /openapi.json  ──────────────────────→ pass through
        │
        ├─ Lazy-scan app routes for __shield_meta__ (once only)
        │
        ├─ @force_active route? ──────────────────────────────→ pass through
        │
        ├─ engine.check(path, method)
        │       │
        │       ├─ Global maintenance ON? → 503
        │       ├─ MAINTENANCE  → 503 + Retry-After header
        │       ├─ DISABLED     → 503
        │       ├─ ENV_GATED    → 403 JSON
        │       ├─ DEPRECATED   → pass through + inject response headers
        │       ├─ ACTIVE       → pass through ✓
        │       │
        │       └─ Rate limit check (if policy registered for route)
        │               ├─ Exempt IP or role? → pass through ✓
        │               ├─ Under limit?       → pass through + X-RateLimit-* headers ✓
        │               └─ Limit exceeded?    → 429 + Retry-After header
        │
        └─ call_next(request)
```

---

## Route registration

The middleware auto-registers routes on first startup by scanning for `__shield_meta__` on route handlers. This works with any router type: plain `APIRouter`, `ShieldRouter`, or routes added directly to the app.

If a route already has persisted state in the backend (for example, written by a previous CLI command), the decorator default is **ignored** and the persisted state wins. This means runtime changes survive restarts.

---

## Paths excluded from checks

The following paths always pass through regardless of shield state:

- `/docs`, `/redoc`, `/openapi.json`: API documentation
- `/shield/`: admin dashboard prefix

You can exclude additional paths by using `@force_active` on those routes.

---

## Global maintenance mode

The middleware also enforces **global** maintenance mode, a single switch that blocks every route at once:

```python
# Block everything immediately
await engine.enable_global_maintenance(
    reason="Emergency patch — back in 15 minutes",
    exempt_paths=["/health", "GET:/admin/status"],
)

# All non-exempt routes now return 503
# Restore normal operation
await engine.disable_global_maintenance()
```

See [**Reference: ShieldEngine**](../reference/engine.md) for the full global maintenance API.

---

## Response format

All error responses from the middleware use a consistent JSON structure:

```json
{
  "error": {
    "code": "MAINTENANCE_MODE",
    "message": "This endpoint is temporarily unavailable",
    "reason": "Database migration in progress",
    "path": "/api/payments",
    "retry_after": "2025-06-01T04:00:00Z"
  }
}
```

| Scenario | Status | `code` |
|---|---|---|
| Maintenance mode | 503 | `MAINTENANCE_MODE` |
| Route disabled | 503 | `ROUTE_DISABLED` |
| Env-gated (wrong env) | 403 | `ENV_GATED` |
| Global maintenance | 503 | `MAINTENANCE_MODE` |
| Rate limit exceeded | 429 | `RATE_LIMIT_EXCEEDED` |

---

## OpenAPI integration (FastAPI only)

FastAPI exposes a live OpenAPI schema at `/openapi.json`. Shield can filter it to hide disabled and env-gated routes and annotate maintained or deprecated ones:

```python
from shield.fastapi.openapi import apply_shield_to_openapi

apply_shield_to_openapi(app, engine)  # call after add_middleware
```

For enhanced docs UI with maintenance banners:

```python
from shield.fastapi.openapi import apply_shield_to_openapi, setup_shield_docs

apply_shield_to_openapi(app, engine)  # must come first
setup_shield_docs(app, engine)        # inject banners into /docs and /redoc
```

See [**Reference: Middleware**](../reference/middleware.md) for all parameters.

---

## Next step

[**Tutorial: Backends →**](backends.md)
