# api-shield

**Route lifecycle management for FastAPI — maintenance mode, environment gating, deprecation, canary rollouts, and more. No restarts required.**

Most "maintenance mode" tools are blunt instruments: shut everything down or nothing at all. `api-shield` treats each route as a first-class entity with its own lifecycle. State changes take effect immediately through an ASGI middleware — no redeployment, no server restart.

---

## Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Decorators](#decorators)
- [Global Maintenance Mode](#global-maintenance-mode)
- [Backends](#backends)
- [OpenAPI & Docs Integration](#openapi--docs-integration)
- [CLI Reference](#cli-reference)
- [Audit Log](#audit-log)
- [Configuration File](#configuration-file)
- [Architecture](#architecture)
- [Testing](#testing)

---

## Quick Start

```bash
uv add api-shield
# or: pip install api-shield
```

```python
from fastapi import FastAPI
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware,
    apply_shield_to_openapi,
    setup_shield_docs,
    maintenance,
    env_only,
    disabled,
    force_active,
    deprecated,
)

engine = make_engine()  # reads SHIELD_BACKEND, SHIELD_ENV, etc.

app = FastAPI(title="My API")
app.add_middleware(ShieldMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="Database migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active                        # always 200, immune to all shield checks
async def health():
    return {"status": "ok"}

@app.get("/debug")
@env_only("dev", "staging")         # silent 404 in production
async def debug():
    return {"debug": True}

@app.get("/old-endpoint")
@disabled(reason="Use /v2/endpoint")
async def old_endpoint():
    return {}

@app.get("/v1/users")
@deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/users")
async def v1_users():
    return {"users": []}

apply_shield_to_openapi(app, engine) # filter /docs and /redoc
setup_shield_docs(app, engine)       # inject maintenance banners into UI
```

```
GET /payments      → 503  {"error": {"code": "MAINTENANCE_MODE", "reason": "..."}}
GET /health        → 200  always (force_active)
GET /debug         → 404  in production (env_only)
GET /old-endpoint  → 503  {"error": {"code": "ROUTE_DISABLED", "reason": "..."}}
GET /v1/users      → 200  + Deprecation/Sunset/Link response headers
```

---

## How It Works

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
        │   (unless global maintenance overrides — see below)
        │
        ├─ engine.check(path, method)
        │       │
        │       ├─ Global maintenance ON + path not exempt? → 503
        │       ├─ MAINTENANCE  → 503 + Retry-After header
        │       ├─ DISABLED     → 503
        │       ├─ ENV_GATED    → 404 (silent — path existence not revealed)
        │       ├─ DEPRECATED   → pass through + inject response headers
        │       └─ ACTIVE       → pass through ✓
        │
        └─ call_next(request)
```

### Route Registration

Shield decorators stamp `__shield_meta__` on the endpoint function. This metadata is registered with the engine at startup via two mechanisms:

1. **ASGI lifespan interception** — `ShieldMiddleware` hooks into `lifespan.startup.complete` to scan all app routes before the first request. This works with any `APIRouter` (plain or `ShieldRouter`).
2. **Lazy fallback** — on the first HTTP request if no lifespan was triggered (e.g. test environments).

State registration is **persistence-first**: if the backend already has a state for a route (written by a previous CLI command or earlier server run), the decorator default is ignored and the persisted state wins. This means runtime changes survive restarts.

---

## Decorators

All decorators work on any router type — plain `APIRouter`, `ShieldRouter`, or routes added directly to the `FastAPI` app instance.

### `@maintenance(reason, start, end)`

Puts a route into maintenance mode. Returns 503 with a structured JSON body. If `start`/`end` are provided, the maintenance window is also stored for scheduling.

```python
from shield.fastapi import maintenance
from datetime import datetime, UTC

@router.get("/payments")
@maintenance(reason="DB migration in progress")
async def get_payments():
    ...

# With a scheduled window
@router.post("/orders")
@maintenance(
    reason="Order system upgrade",
    start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
    end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
)
async def create_order():
    ...
```

Response:
```json
{
  "error": {
    "code": "MAINTENANCE_MODE",
    "message": "This endpoint is temporarily unavailable",
    "reason": "DB migration in progress",
    "path": "/payments",
    "retry_after": "2025-06-01T04:00:00Z"
  }
}
```

---

### `@disabled(reason)`

Permanently disables a route. Returns 503. Use for routes that should never be called again (migrations, removed features).

```python
from shield.fastapi import disabled

@router.get("/legacy/report")
@disabled(reason="Replaced by /v2/reports — update your clients")
async def legacy_report():
    ...
```

---

### `@env_only(*envs)`

Restricts a route to specific environment names. In any other environment the route returns a **silent 404** — it does not reveal that the path exists.

```python
from shield.fastapi import env_only

@router.get("/internal/metrics")
@env_only("dev", "staging")
async def internal_metrics():
    ...
```

The current environment is set via `SHIELD_ENV` or when constructing the engine:

```python
engine = ShieldEngine(current_env="production")
# or
engine = make_engine(current_env="staging")
```

---

### `@force_active`

Bypasses all shield checks. Use for health checks, status endpoints, and any route that must always be reachable.

```python
from shield.fastapi import force_active

@router.get("/health")
@force_active
async def health():
    return {"status": "ok"}
```

`@force_active` routes are also **immune to runtime changes** — you cannot disable or put them in maintenance via the CLI or engine. This is intentional: health check routes must be trustworthy.

The only exception is when global maintenance mode is enabled with `include_force_active=True` (see [Global Maintenance Mode](#global-maintenance-mode)).

---

### `@deprecated(sunset, use_instead)`

Marks a route as deprecated. Requests still succeed, but the middleware injects RFC-compliant response headers:

```python
from shield.fastapi import deprecated

@router.get("/v1/users")
@deprecated(
    sunset="Sat, 01 Jan 2027 00:00:00 GMT",
    use_instead="/v2/users",
)
async def v1_users():
    return {"users": []}
```

Response headers added automatically:
```
Deprecation: true
Sunset: Sat, 01 Jan 2027 00:00:00 GMT
Link: </v2/users>; rel="successor-version"
```

The route is also marked `deprecated: true` in the OpenAPI schema and shown with a visual indicator in `/docs`.

---

## Global Maintenance Mode

Global maintenance blocks **every route** with a single call, without requiring per-route decorators. Use it for full deployments, infrastructure work, or emergency stops.

### Programmatic (lifespan or runtime)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Enable global maintenance at startup
    await engine.enable_global_maintenance(
        reason="Scheduled deployment — back in 15 minutes",
        exempt_paths=["/health", "GET:/admin/status"],
        include_force_active=False,  # @force_active routes still bypass (default)
    )
    yield
    await engine.disable_global_maintenance()
```

Or toggle at runtime via any async context:

```python
# Enable — all non-exempt routes return 503 immediately
await engine.enable_global_maintenance(reason="Emergency patch")

# Disable — routes return to their per-route state
await engine.disable_global_maintenance()

# Check current state
cfg = await engine.get_global_maintenance()
print(cfg.enabled, cfg.reason, cfg.exempt_paths)

# Add/remove individual exemptions without toggling the mode
await engine.set_global_exempt_paths(["/health", "/status"])
```

### Via CLI

```bash
# Enable with exemptions
shield global enable \
  --reason "Scheduled deployment" \
  --exempt /health \
  --exempt GET:/admin/status

# Block even force_active routes
shield global enable --reason "Hard lockdown" --include-force-active

# Add/remove exemptions while maintenance is already active
shield global exempt-add /monitoring/ping
shield global exempt-remove /monitoring/ping

# Check current state
shield global status

# Disable
shield global disable
```

### Options

| Option | Default | Description |
|---|---|---|
| `reason` | `""` | Shown in every 503 response body |
| `exempt_paths` | `[]` | Bare paths (`/health`) or method-prefixed (`GET:/health`) |
| `include_force_active` | `False` | When `True`, `@force_active` routes are also blocked |

---

## Backends

The backend determines where route state and the audit log are persisted.

### `MemoryBackend` (default)

In-process dict. No persistence across restarts. CLI cannot share state with the running server.

```python
from shield.core.backends.memory import MemoryBackend
engine = ShieldEngine(backend=MemoryBackend())
```

Best for: development, single-process testing.

---

### `FileBackend`

JSON file on disk. Survives restarts. CLI shares state with the running server when both point to the same file.

```python
from shield.core.backends.file import FileBackend
engine = ShieldEngine(backend=FileBackend(path="shield-state.json"))
```

Or via environment variable:
```bash
SHIELD_BACKEND=file SHIELD_FILE_PATH=./shield-state.json uvicorn app:app
```

Best for: single-instance deployments, simple setups, CLI-driven workflows.

---

### `RedisBackend`

Redis via `redis-py` async. Supports multi-instance deployments. CLI changes reflect immediately on all running instances.

```python
from shield.core.backends.redis import RedisBackend
engine = ShieldEngine(backend=RedisBackend(url="redis://localhost:6379/0"))
```

Or via environment variable:
```bash
SHIELD_BACKEND=redis SHIELD_REDIS_URL=redis://localhost:6379/0 uvicorn app:app
```

Key schema:
- `shield:state:{path}` — route state
- `shield:audit` — audit log (LPUSH, capped at 1000 entries)
- `shield:global` — global maintenance configuration

Best for: multi-instance / load-balanced deployments, production.

---

### Config file (`.shield`)

Both the app and CLI auto-discover a `.shield` file by walking up from the current directory:

```ini
# .shield
SHIELD_BACKEND=file
SHIELD_FILE_PATH=shield-state.json
SHIELD_ENV=production
```

Priority order (highest wins):
1. Explicit constructor arguments
2. `os.environ`
3. `.shield` file
4. Built-in defaults

Pass a specific config file to the CLI:
```bash
shield --config /etc/myapp/.shield status
```

---

## OpenAPI & Docs Integration

### Schema filtering

```python
from shield.fastapi import apply_shield_to_openapi

apply_shield_to_openapi(app, engine)
```

Effect on `/docs` and `/redoc`:

| Route status | Schema behaviour |
|---|---|
| `DISABLED` | Hidden from all schemas |
| `ENV_GATED` (wrong env) | Hidden from all schemas |
| `MAINTENANCE` | Visible; operation summary prefixed with `🔧`; description shows warning block; `x-shield-status` extension added |
| `DEPRECATED` | Marked `deprecated: true`; successor path shown |
| `ACTIVE` | No change |

Schema is computed fresh on every request — runtime state changes (CLI, engine calls) reflect immediately without restarting.

---

### Docs UI customisation

```python
from shield.fastapi import setup_shield_docs

apply_shield_to_openapi(app, engine)  # must come first
setup_shield_docs(app, engine)
```

Replaces both `/docs` and `/redoc` with enhanced versions:

**Global maintenance ON:**
- Full-width pulsing red sticky banner at the top of the page
- Reason text and exempt paths displayed
- Refreshes automatically every 15 seconds — no page reload needed

**Global maintenance OFF:**
- Small green "All systems operational" chip in the bottom-right corner

**Per-route maintenance:**
- Orange left-border on the operation block
- `🔧 MAINTENANCE` badge appended to the summary bar

---

## CLI Reference

The `shield` CLI operates on the same backend as the running server. Requires `SHIELD_BACKEND=file` or `SHIELD_BACKEND=redis` to share state (the default `memory` backend is process-local).

```bash
# Install entry point
uv pip install -e ".[cli]"
```

### Route commands

```bash
# Show all registered routes
shield status

# Show one route
shield status GET:/payments

# Enable a route
shield enable GET:/payments

# Disable with a reason
shield disable GET:/payments --reason "Security patch"

# Put in maintenance (immediate)
shield maintenance GET:/payments --reason "DB swap"

# Put in maintenance with a time window
shield maintenance GET:/payments \
  --reason "DB migration" \
  --start 2025-06-01T02:00Z \
  --end 2025-06-01T04:00Z

# Schedule a future maintenance window (auto-activates and deactivates)
shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end 2025-06-01T04:00Z \
  --reason "Planned migration"
```

### Global maintenance commands

```bash
shield global status
shield global enable --reason "Deploying v2" --exempt /health
shield global disable
shield global exempt-add /monitoring
shield global exempt-remove /monitoring
```

### Audit log

```bash
shield log                          # last 20 entries across all routes
shield log --route GET:/payments    # filter by route
shield log --limit 100
```

### Notes on route keys

Routes are stored with method-prefixed keys:

| What you type | What gets stored |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |
| `@router.get("/api/v1/users")` | `GET:/api/v1/users` |

Use the same format with the CLI:
```bash
shield disable "GET:/payments"
shield enable "/payments"           # applies to all methods
```

---

## Audit Log

Every state change writes an immutable audit entry:

```python
# Via engine
entries = await engine.get_audit_log(limit=50)
entries = await engine.get_audit_log(path="GET:/payments", limit=20)

for e in entries:
    print(e.timestamp, e.actor, e.action, e.path,
          e.previous_status, "→", e.new_status, e.reason)
```

Fields: `id`, `timestamp`, `path`, `action`, `actor`, `reason`, `previous_status`, `new_status`.

The CLI uses `getpass.getuser()` (the logged-in OS username) as the default actor — no `--actor` flag needed for accountability:

```bash
shield disable GET:/payments --reason "Security patch"
# audit entry: actor="alice", action="disable", path="GET:/payments"
```

---

## Architecture

```
shield/
├── core/                       # Framework-agnostic — zero FastAPI imports
│   ├── models.py               # RouteState, AuditEntry, GlobalMaintenanceConfig
│   ├── engine.py               # ShieldEngine — all business logic
│   ├── scheduler.py            # MaintenanceScheduler (asyncio.Task based)
│   ├── config.py               # Backend/engine factory + .shield file loading
│   ├── exceptions.py           # MaintenanceException, EnvGatedException, ...
│   └── backends/
│       ├── base.py             # ShieldBackend ABC
│       ├── memory.py           # In-process dict
│       ├── file.py             # JSON file via aiofiles
│       └── redis.py            # Redis via redis-py async
│
├── fastapi/                    # FastAPI adapter layer
│   ├── middleware.py           # ShieldMiddleware (ASGI, BaseHTTPMiddleware)
│   ├── decorators.py           # @maintenance, @disabled, @env_only, ...
│   ├── router.py               # ShieldRouter + scan_routes()
│   └── openapi.py              # Schema filter + docs UI customisation
│
└── cli/
    └── main.py                 # Typer CLI app
```

### Key design rules

1. **`shield.core` never imports from `shield.fastapi`** — the core is framework-agnostic and can power future adapters (Flask, Litestar, Django).
2. **All business logic lives in `ShieldEngine`** — middleware and decorators are transport layers that call `engine.check()`, never make policy decisions themselves.
3. **`engine.check()` is the single chokepoint** — every request, regardless of router type, goes through this one method.
4. **Fail-open on backend errors** — if the backend is unreachable, requests pass through. Shield never takes down an API due to its own failures.
5. **Persistence-first registration** — if a route already has persisted state, the decorator default is ignored. Runtime changes survive restarts.

---

## Testing

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test file
uv run pytest tests/fastapi/test_middleware.py

# Run only core tests (no FastAPI dependency)
uv run pytest tests/core/
```

### Writing tests with shield

```python
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.fastapi.decorators import maintenance, force_active
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.router import ShieldRouter


async def test_maintenance_returns_503():
    engine = ShieldEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(ShieldMiddleware, engine=engine)
    router = ShieldRouter(engine=engine)

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments():
        return {"ok": True}

    app.include_router(router)
    await app.router.startup()   # trigger shield route registration

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/payments")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MAINTENANCE_MODE"


async def test_runtime_enable_via_engine():
    engine = ShieldEngine(backend=MemoryBackend())
    # ... set up app ...

    # Put a route in maintenance at runtime (no decorator needed)
    await engine.set_maintenance("GET:/orders", reason="Upgrade")

    # Re-enable it
    await engine.enable("GET:/orders")

    state = await engine.get_state("GET:/orders")
    assert state.status.value == "active"
```

### Test configuration

`pyproject.toml` includes:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"   # all async tests work without @pytest.mark.asyncio
```

---

## Error Response Format

All shield-generated error responses follow a consistent JSON structure:

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

| Scenario | HTTP status | `code` |
|---|---|---|
| Route in maintenance | 503 | `MAINTENANCE_MODE` |
| Route disabled | 503 | `ROUTE_DISABLED` |
| Route env-gated (wrong env) | 404 | *(no body — silent)* |
| Global maintenance active | 503 | `MAINTENANCE_MODE` |
