# FastAPI Adapter

The FastAPI adapter provides middleware, decorators, a drop-in router, and OpenAPI integration — all built on top of the framework-agnostic `waygate.core`.

!!! info "More adapters on the way"
    We currently support FastAPI. Other framework adapters are on the way. [Open an issue](https://github.com/Attakay78/waygate/issues) if you'd like to see your framework supported sooner.

---

## Installation

```bash
uv add "waygate[fastapi]"              # adapter only
uv add "waygate[fastapi,rate-limit]"   # with rate limiting
uv add "waygate[all]"                  # everything including CLI + admin
```

---

## Quick setup

```python title="main.py"
from fastapi import FastAPI
from waygate import make_engine
from waygate.fastapi import (
    WaygateMiddleware,
    WaygateAdmin,
    apply_waygate_to_openapi,
    setup_waygate_docs,
    maintenance,
    env_only,
    disabled,
    force_active,
    deprecated,
)

engine = make_engine()  # reads WAYGATE_BACKEND, WAYGATE_ENV from env / .waygate

app = FastAPI()
app.add_middleware(WaygateMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active
async def health():
    return {"status": "ok"}

apply_waygate_to_openapi(app, engine)
setup_waygate_docs(app, engine)

app.mount("/waygate", WaygateAdmin(engine=engine, auth=("admin", "secret")))
```

---

## Components

### WaygateMiddleware

ASGI middleware that enforces route state on every request.

```python
app.add_middleware(WaygateMiddleware, engine=engine)
```

See [**Reference: Middleware**](../reference/middleware.md) for full details.

---

### Decorators

All decorators work with any router type (plain `APIRouter`, `WaygateRouter`, or routes added directly to `app`).

| Decorator | Import | Behaviour |
|---|---|---|
| `@maintenance(reason, start, end)` | `waygate.fastapi` | 503 temporarily |
| `@disabled(reason)` | `waygate.fastapi` | 503 permanently |
| `@env_only(*envs)` | `waygate.fastapi` | 404 in other envs |
| `@deprecated(sunset, use_instead)` | `waygate.fastapi` | 200 + headers |
| `@force_active` | `waygate.fastapi` | Always 200 |
| `@rate_limit("100/minute")` | `waygate.fastapi.decorators` | 429 when exceeded |

See [**Reference: Decorators**](../reference/decorators.md) for full details.

---

### WaygateRouter

A drop-in replacement for `APIRouter` that automatically registers route metadata with the engine at startup.

```python
from waygate.fastapi import WaygateRouter

router = WaygateRouter(engine=engine)

@router.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

app.include_router(router)
```

!!! note
    `WaygateRouter` is optional. `WaygateMiddleware` also registers routes by scanning `app.routes` at startup (lazy, on first request). Use `WaygateRouter` for explicit control over registration order.

---

### WaygateAdmin

Mounts the admin dashboard UI and the REST API (used by the `waygate` CLI) under a single path.

```python
from waygate.fastapi import WaygateAdmin

app.mount("/waygate", WaygateAdmin(engine=engine, auth=("admin", "secret")))
```

See [**Tutorial: Admin Dashboard**](../tutorial/admin-dashboard.md) for full details.

---

## Rate limiting

Requires `waygate[rate-limit]` on the server.

```python
from waygate.fastapi import rate_limit

@router.get("/public/posts")
@rate_limit("10/minute")               # 10 req/min per IP
async def list_posts():
    return {"posts": []}

@router.get("/users/me")
@rate_limit("100/minute", key="user")  # per authenticated user
async def get_current_user():
    ...

@router.get("/reports")
@rate_limit(                           # tiered limits
    {"free": "10/minute", "pro": "100/minute", "enterprise": "unlimited"},
    key="user",
)
async def get_reports():
    ...
```

Custom response on rate limit violations:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
from waygate import RateLimitExceededException

def my_429(request: Request, exc: RateLimitExceededException) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "retry_after": exc.retry_after_seconds},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )

@router.get("/posts")
@rate_limit("10/minute", response=my_429)
async def list_posts(): ...
```

Global default (applies to all rate-limited routes without a per-route factory):

```python
app.add_middleware(
    WaygateMiddleware,
    engine=engine,
    responses={"rate_limited": my_429},
)
```

Mutate policies at runtime without redeploying (`waygate rl` and `waygate rate-limits` are aliases):

```bash
waygate rl set GET:/public/posts 20/minute
waygate rl reset GET:/public/posts
waygate rl hits
```

See [**Tutorial: Rate Limiting**](../tutorial/rate-limiting.md) and [**Reference: Rate Limiting**](../reference/rate-limiting.md) for full details.

---

## Dependency injection

Waygate decorators work as FastAPI `Depends()` dependencies for per-handler enforcement without middleware.

```python title="two patterns"
from fastapi import Depends
from waygate.fastapi import disabled, maintenance

# Pattern A — decorator (relies on WaygateMiddleware to enforce)
@router.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

# Pattern B — Depends() only (per-handler, no middleware required)
@router.get("/admin/report", dependencies=[Depends(disabled(reason="Use /v2/report"))])
async def admin_report():
    return {}

@router.get("/orders", dependencies=[Depends(maintenance(reason="Order upgrade"))])
async def get_orders():
    return {"orders": []}
```

`@rate_limit` also works as a `Depends()`:

```python
from waygate.fastapi import rate_limit

@router.get("/export", dependencies=[Depends(rate_limit("5/hour", key="user"))])
async def export():
    ...
```

Both the decorator path and the `Depends()` path share the same counter — they are equivalent in enforcement.

| Pattern | Best for |
|---|---|
| Decorator | Apps that always run `WaygateMiddleware` |
| `Depends()` | Serverless / edge runtimes without middleware, or when middleware is not used |

---

## Using with FastAPI lifespan

```python title="main.py"
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:   # calls backend.startup() then backend.shutdown()
        yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(WaygateMiddleware, engine=engine)
```

---

## Testing

```python title="tests/test_payments.py"
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from waygate import MemoryBackend
from waygate import WaygateEngine
from waygate.fastapi import maintenance, force_active
from waygate.fastapi import WaygateMiddleware
from waygate.fastapi import WaygateRouter


async def test_maintenance_returns_503():
    engine = WaygateEngine(backend=MemoryBackend())
    app = FastAPI()
    app.add_middleware(WaygateMiddleware, engine=engine)
    router = WaygateRouter(engine=engine)

    @router.get("/payments")
    @maintenance(reason="DB migration")
    async def get_payments():
        return {"ok": True}

    app.include_router(router)
    await app.router.startup()   # trigger waygate route registration

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/payments")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MAINTENANCE_MODE"


async def test_runtime_enable_via_engine():
    engine = WaygateEngine(backend=MemoryBackend())

    await engine.set_maintenance("GET:/orders", reason="Upgrade")
    await engine.enable("GET:/orders")

    state = await engine.get_state("GET:/orders")
    assert state.status.value == "active"
```

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"   # all async tests work without @pytest.mark.asyncio
```

---

## Runnable examples

Each example below is a complete, self-contained FastAPI app. Click to expand the full source, then copy and run it locally.

---

### Basic usage

??? example "All core decorators + WaygateAdmin"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/basic.py){ .md-button }

    Demonstrates every decorator (`@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated`) together with the `WaygateAdmin` unified interface (dashboard + CLI REST API).

    **Expected behavior:**

    | Endpoint | Response | Why |
    |---|---|---|
    | `GET /health` | 200 always | `@force_active` |
    | `GET /payments` | 503 `MAINTENANCE_MODE` | `@maintenance` |
    | `GET /debug` | 200 in dev, 404 in production | `@env_only("dev")` |
    | `GET /old-endpoint` | 503 `ROUTE_DISABLED` | `@disabled` |
    | `GET /v1/users` | 200 + `Deprecation` headers | `@deprecated` |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.basic:app --reload
    # Swagger UI:       http://localhost:8000/docs
    # Admin dashboard:  http://localhost:8000/waygate/   (admin / secret)
    # Audit log:        http://localhost:8000/waygate/audit
    ```

    **CLI quick-start:**

    ```bash
    waygate login admin          # password: secret
    waygate status
    waygate disable GET:/payments --reason "hotfix"
    waygate enable  GET:/payments
    ```

    **Full source:**

    ```python title="examples/fastapi/basic.py"
    --8<-- "examples/fastapi/basic.py"
    ```

---

### Dependency injection

??? example "Waygate decorators as FastAPI `Depends()`"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/dependency_injection.py){ .md-button }

    Shows how to use waygate decorators as `Depends()` instead of (or alongside) middleware. Once `configure_waygate(app, engine)` is called — or `WaygateMiddleware` is added, which calls it automatically — all decorator dependencies find the engine via `request.app.state` without needing an explicit `engine=` argument per route.

    **Expected behavior:**

    | Endpoint | Response |
    |---|---|
    | `GET /payments` | 503 (maintenance) — toggle off with `waygate enable GET:/payments` |
    | `GET /old-endpoint` | 503 (disabled) |
    | `GET /debug` | 404 in production, 200 in dev/staging |
    | `GET /v1/users` | 200 + `Deprecation` / `Sunset` / `Link` headers |
    | `GET /health` | 200 always |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.dependency_injection:app --reload
    # Admin dashboard: http://localhost:8000/waygate/   (admin / secret)
    ```

    **Try it:**

    ```bash
    curl -i http://localhost:8000/payments      # → 503
    waygate enable GET:/payments                 # toggle off without redeploy
    curl -i http://localhost:8000/payments      # → 200
    ```

    **Full source:**

    ```python title="examples/fastapi/dependency_injection.py"
    --8<-- "examples/fastapi/dependency_injection.py"
    ```

---

### Scheduled maintenance

??? example "Auto-activating and auto-deactivating windows"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/scheduled_maintenance.py){ .md-button }

    Demonstrates how to schedule a maintenance window that activates and deactivates automatically at the specified times — no manual intervention required.

    **Endpoints:**

    | Endpoint | Purpose |
    |---|---|
    | `GET /orders` | Normal route — enters maintenance during the window |
    | `GET /admin/schedule` | Schedules a 10-second window starting 5 seconds from now |
    | `GET /admin/status` | Current waygate state for all routes |
    | `GET /health` | Always 200 |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.scheduled_maintenance:app --reload
    ```

    **Quick demo:**

    ```bash
    # 1. Route is active
    curl http://localhost:8000/orders            # → 200

    # 2. Schedule the window (activates in 5 s, ends in 15 s)
    curl http://localhost:8000/admin/schedule

    # 3. Wait 5 seconds, then:
    curl http://localhost:8000/orders            # → 503 MAINTENANCE_MODE

    # 4. Wait 10 more seconds, then:
    curl http://localhost:8000/orders            # → 200 again
    ```

    **Full source:**

    ```python title="examples/fastapi/scheduled_maintenance.py"
    --8<-- "examples/fastapi/scheduled_maintenance.py"
    ```

---

### Global maintenance

??? example "Blocking all routes at once"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/global_maintenance.py){ .md-button }

    Demonstrates enabling and disabling global maintenance mode, which blocks every route in one call without per-route decorators. `@force_active` routes are exempt by default.

    **Endpoints:**

    | Endpoint | Purpose |
    |---|---|
    | `GET /payments` | Normal business route — blocked during global maintenance |
    | `GET /orders` | Normal business route — blocked during global maintenance |
    | `GET /health` | Always 200 (`@force_active` bypasses global maintenance) |
    | `GET /admin/on` | Enable global maintenance |
    | `GET /admin/off` | Disable global maintenance |
    | `GET /admin/status` | Current global maintenance config |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.global_maintenance:app --reload
    ```

    **Quick demo:**

    ```bash
    curl http://localhost:8000/payments          # → 200
    curl http://localhost:8000/admin/on          # enable global maintenance
    curl http://localhost:8000/payments          # → 503 MAINTENANCE_MODE
    curl http://localhost:8000/health            # → 200 (force_active, exempt)
    curl http://localhost:8000/admin/off         # restore normal operation
    curl http://localhost:8000/payments          # → 200
    ```

    **Full source:**

    ```python title="examples/fastapi/global_maintenance.py"
    --8<-- "examples/fastapi/global_maintenance.py"
    ```

---

### Custom responses

??? example "HTML pages, redirects, and branded JSON errors"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/custom_responses.py){ .md-button }

    Shows how to replace the default JSON error body with any Starlette response — HTML maintenance pages, redirects, plain text, or a different JSON shape — either per-route or as an app-wide default on the middleware.

    **Resolution order:** per-route `response=` → global `responses=` default → built-in JSON.

    **Expected behavior:**

    | Endpoint | Response | How |
    |---|---|---|
    | `GET /payments` | HTML maintenance page | Per-route factory on `@maintenance` |
    | `GET /orders` | 302 redirect to `/status` | Per-route lambda on `@maintenance` |
    | `GET /legacy` | Plain text 503 | Per-route lambda on `@disabled` |
    | `GET /inventory` | HTML from global default | No per-route factory; falls back to middleware |
    | `GET /reports` | HTML from global async factory | No per-route factory; async fallback |
    | `GET /status` | 200 JSON | Redirect target (`@force_active`) |
    | `GET /health` | 200 JSON | `@force_active` |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.custom_responses:app --reload
    # Admin dashboard: http://localhost:8000/waygate/   (admin / secret)
    ```

    **Full source:**

    ```python title="examples/fastapi/custom_responses.py"
    --8<-- "examples/fastapi/custom_responses.py"
    ```

---

### Webhooks

??? example "HTTP notifications on every state change"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/webhooks.py){ .md-button }

    Fully self-contained webhook demo: three receivers (generic JSON, Slack-formatted, and a custom payload) are mounted on the same app — no external service needed. Change a route state via the CLI or dashboard and watch the events appear at `/webhook-log`.

    Webhooks are always registered on the engine that owns state mutations:

    - **Embedded mode** — register on the engine before passing it to `WaygateAdmin`
    - **Waygate Server mode** — build the engine explicitly and register on it before passing to `WaygateAdmin`; SDK service apps never fire webhooks

    ```python
    # Waygate Server mode
    from waygate import WaygateEngine
    from waygate import SlackWebhookFormatter
    from waygate.fastapi import WaygateAdmin

    engine = WaygateEngine(backend=RedisBackend(...))
    engine.add_webhook("https://hooks.slack.com/...", formatter=SlackWebhookFormatter())
    waygate_app = WaygateAdmin(engine=engine, auth=("admin", "secret"))
    ```

    **Webhook receivers (all `@force_active`):**

    | Endpoint | Payload format |
    |---|---|
    | `POST /webhooks/generic` | Default JSON (`default_formatter`) |
    | `POST /webhooks/slack` | Slack Incoming Webhook blocks (`SlackWebhookFormatter`) |
    | `POST /webhooks/custom` | Bespoke minimal payload (custom formatter) |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.webhooks:app --reload
    # Webhook log: http://localhost:8000/webhook-log  (auto-refreshes every 5 s)
    # Admin:       http://localhost:8000/waygate/       (admin / secret)
    ```

    **Trigger events:**

    ```bash
    waygate config set-url http://localhost:8000/waygate
    waygate login admin                                   # password: secret
    waygate disable GET:/payments --reason "hotfix"
    waygate enable  GET:/payments
    waygate maintenance GET:/orders --reason "stock sync"
    waygate enable  GET:/orders
    ```

    Then open `http://localhost:8000/webhook-log` to see all three receivers fire for each state change.

    **Full source:**

    ```python title="examples/fastapi/webhooks.py"
    --8<-- "examples/fastapi/webhooks.py"
    ```

---

### Rate limiting

??? example "Per-IP, per-user, tiered limits, and custom 429 responses"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/rate_limiting.py){ .md-button }

    Demonstrates IP-based, user-based, and tiered rate limiting with a custom 429 response factory. Requires `waygate[rate-limit]`.

    **Expected behavior:**

    | Endpoint | Limit | Key |
    |---|---|---|
    | `GET /public/posts` | 5/minute | IP |
    | `GET /users/me` | 20/minute | user |
    | `GET /reports` | free: 5/min, pro: 30/min | user + tier |
    | `GET /health` | unlimited | `@force_active` |

    **Run:**

    ```bash
    uv add "waygate[all,rate-limit]"
    uv run uvicorn examples.fastapi.rate_limiting:app --reload
    # Admin dashboard:  http://localhost:8000/waygate/   (admin / secret)
    # Rate limits tab:  http://localhost:8000/waygate/rate-limits
    # Blocked log:      http://localhost:8000/waygate/blocked
    ```

    **CLI quick-start:**

    ```bash
    waygate login admin
    waygate rl list
    waygate rl set GET:/public/posts 20/minute   # raise limit live
    waygate rl reset GET:/public/posts           # clear counters
    waygate rl hits                              # blocked requests log
    ```

    **Full source:**

    ```python title="examples/fastapi/rate_limiting.py"
    --8<-- "examples/fastapi/rate_limiting.py"
    ```

---

### Waygate Server (single service)

??? example "Centralized Waygate Server + one service via WaygateSDK"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/waygate_server.py){ .md-button }

    Demonstrates the centralized Waygate Server architecture: one Waygate Server process owns all route state, and one service app connects via `WaygateSDK`. State is enforced locally — zero per-request network overhead.

    **Two ASGI apps — run each in its own terminal:**

    ```bash
    # Waygate Server (port 8001)
    uv run uvicorn examples.fastapi.waygate_server:waygate_app --port 8001 --reload

    # Service app (port 8000)
    uv run uvicorn examples.fastapi.waygate_server:service_app --port 8000 --reload
    ```

    **Then visit:**

    - `http://localhost:8001/` — Waygate dashboard (`admin` / `secret`)
    - `http://localhost:8000/docs` — service Swagger UI

    **Expected behavior:**

    | Endpoint | Response | Why |
    |---|---|---|
    | `GET /health` | 200 always | `@force_active` |
    | `GET /api/payments` | 503 `MAINTENANCE_MODE` | starts in maintenance |
    | `GET /api/orders` | 200 | active on startup |
    | `GET /api/legacy` | 503 `ROUTE_DISABLED` | `@disabled` |
    | `GET /api/v1/products` | 200 + deprecation headers | `@deprecated` |

    **SDK authentication options:**

    ```python
    # Option 1 — Auto-login (recommended): SDK logs in on startup, no token management
    sdk = WaygateSDK(
        server_url="http://localhost:8001",
        app_id="payments-service",
        username="admin",
        password="secret",   # inject from env in production
    )

    # Option 2 — Pre-issued token
    sdk = WaygateSDK(
        server_url="http://localhost:8001",
        app_id="payments-service",
        token="<token-from-waygate-login>",
    )

    # Option 3 — No auth on the Waygate Server
    sdk = WaygateSDK(server_url="http://localhost:8001", app_id="payments-service")
    ```

    **CLI — always targets the Waygate Server:**

    ```bash
    waygate config set-url http://localhost:8001
    waygate login admin              # password: secret
    waygate status
    waygate enable /api/payments
    waygate disable /api/orders --reason "hotfix"
    waygate maintenance /api/payments --reason "DB migration"
    waygate audit
    ```

    **Full source:**

    ```python title="examples/fastapi/waygate_server.py"
    --8<-- "examples/fastapi/waygate_server.py"
    ```

---

### Waygate Server (multi-service)

??? example "Two independent services sharing one Waygate Server"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/multi_service.py){ .md-button }

    Demonstrates two independent FastAPI services (`payments-service` and `orders-service`) both connecting to the same Waygate Server. Each service registers its routes under its own `app_id` namespace so the dashboard service dropdown and CLI `WAYGATE_SERVICE` env var can manage them independently or together.

    Each service authenticates using `username`/`password` so the SDK obtains its own long-lived `sdk`-platform token on startup — no manual token management required. The Waygate Server is configured with separate expiry times for human sessions and service tokens:

    ```python
    waygate_app = WaygateServer(
        backend=MemoryBackend(),
        auth=("admin", "secret"),
        token_expiry=3600,          # dashboard / CLI: 1 hour
        sdk_token_expiry=31536000,  # SDK services: 1 year
    )

    payments_sdk = WaygateSDK(
        server_url="http://waygate-server:9000",
        app_id="payments-service",
        username="admin",
        password="secret",          # inject from env in production
    )
    ```

    **Three ASGI apps — run each in its own terminal:**

    ```bash
    # Waygate Server (port 8001)
    uv run uvicorn examples.fastapi.multi_service:waygate_app --port 8001 --reload

    # Payments service (port 8000)
    uv run uvicorn examples.fastapi.multi_service:payments_app --port 8000 --reload

    # Orders service (port 8002)
    uv run uvicorn examples.fastapi.multi_service:orders_app --port 8002 --reload
    ```

    **Then visit:**

    - `http://localhost:8001/` — Waygate dashboard (use service dropdown to switch)
    - `http://localhost:8000/docs` — Payments Swagger UI
    - `http://localhost:8002/docs` — Orders Swagger UI

    **Expected behavior:**

    | Service | Endpoint | Response | Why |
    |---|---|---|---|
    | payments | `GET /health` | 200 always | `@force_active` |
    | payments | `GET /api/payments` | 503 `MAINTENANCE_MODE` | starts in maintenance |
    | payments | `GET /api/refunds` | 200 | active |
    | payments | `GET /api/v1/invoices` | 200 + deprecation headers | `@deprecated` |
    | orders | `GET /health` | 200 always | `@force_active` |
    | orders | `GET /api/orders` | 200 | active |
    | orders | `GET /api/shipments` | 503 `ROUTE_DISABLED` | `@disabled` |
    | orders | `GET /api/cart` | 200 | active |

    **CLI — multi-service workflow:**

    ```bash
    waygate config set-url http://localhost:8001
    waygate login admin              # password: secret
    waygate services                 # list all connected services

    # Scope to payments via env var
    export WAYGATE_SERVICE=payments-service
    waygate status
    waygate enable /api/payments
    waygate current-service          # confirm active context

    # Switch to orders with explicit flag (overrides env var)
    waygate status --service orders-service
    waygate disable /api/cart --reason "redesign" --service orders-service

    # Unscoped — operates across all services
    unset WAYGATE_SERVICE
    waygate status
    waygate audit
    waygate global disable --reason "emergency maintenance"
    waygate global enable
    ```

    **Full source:**

    ```python title="examples/fastapi/multi_service.py"
    --8<-- "examples/fastapi/multi_service.py"
    ```

---

### Custom backend (SQLite)

??? example "Implementing `WaygateBackend` with aiosqlite"

    [:material-github: View on GitHub](https://github.com/Attakay78/waygate/blob/main/examples/fastapi/custom_backend/sqlite_backend.py){ .md-button }

    A complete, working custom backend that stores all route state and audit log entries in a SQLite database via `aiosqlite`. Restart the server and the state survives. The same CLI workflow works unchanged — the CLI talks to the app's REST API, never to the database directly.

    **Requirements:**

    ```bash
    uv add aiosqlite
    ```

    **Expected behavior:**

    | Endpoint | Response |
    |---|---|
    | `GET /health` | 200 always — `backend: "sqlite"` in body |
    | `GET /payments` | 503 `MAINTENANCE_MODE` (persisted in SQLite) |
    | `GET /legacy` | 503 `ROUTE_DISABLED` (persisted in SQLite) |
    | `GET /orders` | 200 active |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.custom_backend.sqlite_backend:app --reload
    # Swagger UI:  http://localhost:8000/docs
    # Admin:       http://localhost:8000/waygate/   (admin / secret)
    # Audit log:   http://localhost:8000/waygate/audit
    ```

    **CLI quick-start:**

    ```bash
    waygate config set-url http://localhost:8000/waygate
    waygate login admin          # password: secret
    waygate status
    waygate disable GET:/payments --reason "hotfix"
    waygate enable  GET:/payments
    waygate log
    ```

    **Full source:**

    ```python title="examples/fastapi/custom_backend/sqlite_backend.py"
    --8<-- "examples/fastapi/custom_backend/sqlite_backend.py"
    ```
