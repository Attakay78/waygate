# FastAPI Adapter

The FastAPI adapter is the primary supported adapter. It provides middleware, decorators, a drop-in router, and OpenAPI integration.

---

## Installation

```bash
uv add "api-shield[fastapi]"              # adapter only
uv add "api-shield[fastapi,rate-limit]"   # with rate limiting
uv add "api-shield[all]"                  # everything including CLI + admin
```

---

## Quick setup

```python title="main.py"
from fastapi import FastAPI
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware,
    ShieldAdmin,
    apply_shield_to_openapi,
    setup_shield_docs,
    maintenance,
    env_only,
    disabled,
    force_active,
    deprecated,
)

engine = make_engine()  # reads SHIELD_BACKEND, SHIELD_ENV from env / .shield

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active
async def health():
    return {"status": "ok"}

apply_shield_to_openapi(app, engine)
setup_shield_docs(app, engine)

app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

---

## Components

### ShieldMiddleware

ASGI middleware that enforces route state on every request.

```python
app.add_middleware(ShieldMiddleware, engine=engine)
```

See [**Reference: Middleware**](../reference/middleware.md) for full details.

---

### Decorators

All decorators work with any router type (plain `APIRouter`, `ShieldRouter`, or routes added directly to `app`).

| Decorator | Import | Behaviour |
|---|---|---|
| `@maintenance(reason, start, end)` | `shield.fastapi` | 503 temporarily |
| `@disabled(reason)` | `shield.fastapi` | 503 permanently |
| `@env_only(*envs)` | `shield.fastapi` | 404 in other envs |
| `@deprecated(sunset, use_instead)` | `shield.fastapi` | 200 + headers |
| `@force_active` | `shield.fastapi` | Always 200 |
| `@rate_limit("100/minute")` | `shield.fastapi.decorators` | 429 when exceeded |

See [**Reference: Decorators**](../reference/decorators.md) for full details.

---

### ShieldRouter

A drop-in replacement for `APIRouter` that automatically registers route metadata with the engine at startup.

```python
from shield.fastapi.router import ShieldRouter

router = ShieldRouter(engine=engine)

@router.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

app.include_router(router)
```

!!! note
    `ShieldRouter` is optional. `ShieldMiddleware` also registers routes by scanning `app.routes` at startup (lazy, on first request). Use `ShieldRouter` for explicit control over registration order.

---

### ShieldAdmin

Mounts the admin dashboard UI and the REST API (used by the `shield` CLI) under a single path.

```python
from shield.admin import ShieldAdmin

app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

See [**Tutorial: Admin Dashboard**](../tutorial/admin-dashboard.md) for full details.

---

## Rate limiting

Requires `api-shield[rate-limit]` on the server.

```python
from shield.fastapi.decorators import rate_limit

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
from shield.core.exceptions import RateLimitExceededException

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
    ShieldMiddleware,
    engine=engine,
    responses={"rate_limited": my_429},
)
```

Mutate policies at runtime without redeploying (`shield rl` and `shield rate-limits` are aliases):

```bash
shield rl set GET:/public/posts 20/minute
shield rl reset GET:/public/posts
shield rl hits
```

See [**Tutorial: Rate Limiting**](../tutorial/rate-limiting.md) and [**Reference: Rate Limiting**](../reference/rate-limiting.md) for full details.

---

## Dependency injection

Shield decorators work as FastAPI `Depends()` dependencies for per-handler enforcement without middleware.

```python title="two patterns"
from fastapi import Depends
from shield.fastapi.decorators import disabled, maintenance

# Pattern A — decorator (relies on ShieldMiddleware to enforce)
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
from shield.fastapi.decorators import rate_limit

@router.get("/export", dependencies=[Depends(rate_limit("5/hour", key="user"))])
async def export():
    ...
```

Both the decorator path and the `Depends()` path share the same counter — they are equivalent in enforcement.

| Pattern | Best for |
|---|---|
| Decorator | Apps that always run `ShieldMiddleware` |
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
app.add_middleware(ShieldMiddleware, engine=engine)
```

---

## Testing

```python title="tests/test_payments.py"
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

??? example "All core decorators + ShieldAdmin"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/basic.py){ .md-button }

    Demonstrates every decorator (`@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated`) together with the `ShieldAdmin` unified interface (dashboard + CLI REST API).

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
    # Admin dashboard:  http://localhost:8000/shield/   (admin / secret)
    # Audit log:        http://localhost:8000/shield/audit
    ```

    **CLI quick-start:**

    ```bash
    shield login admin          # password: secret
    shield status
    shield disable GET:/payments --reason "hotfix"
    shield enable  GET:/payments
    ```

    **Full source:**

    ```python title="examples/fastapi/basic.py"
    --8<-- "examples/fastapi/basic.py"
    ```

---

### Dependency injection

??? example "Shield decorators as FastAPI `Depends()`"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/dependency_injection.py){ .md-button }

    Shows how to use shield decorators as `Depends()` instead of (or alongside) middleware. Once `configure_shield(app, engine)` is called — or `ShieldMiddleware` is added, which calls it automatically — all decorator dependencies find the engine via `request.app.state` without needing an explicit `engine=` argument per route.

    **Expected behavior:**

    | Endpoint | Response |
    |---|---|
    | `GET /payments` | 503 (maintenance) — toggle off with `shield enable GET:/payments` |
    | `GET /old-endpoint` | 503 (disabled) |
    | `GET /debug` | 404 in production, 200 in dev/staging |
    | `GET /v1/users` | 200 + `Deprecation` / `Sunset` / `Link` headers |
    | `GET /health` | 200 always |

    **Run:**

    ```bash
    uv run uvicorn examples.fastapi.dependency_injection:app --reload
    # Admin dashboard: http://localhost:8000/shield/   (admin / secret)
    ```

    **Try it:**

    ```bash
    curl -i http://localhost:8000/payments      # → 503
    shield enable GET:/payments                 # toggle off without redeploy
    curl -i http://localhost:8000/payments      # → 200
    ```

    **Full source:**

    ```python title="examples/fastapi/dependency_injection.py"
    --8<-- "examples/fastapi/dependency_injection.py"
    ```

---

### Scheduled maintenance

??? example "Auto-activating and auto-deactivating windows"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/scheduled_maintenance.py){ .md-button }

    Demonstrates how to schedule a maintenance window that activates and deactivates automatically at the specified times — no manual intervention required.

    **Endpoints:**

    | Endpoint | Purpose |
    |---|---|
    | `GET /orders` | Normal route — enters maintenance during the window |
    | `GET /admin/schedule` | Schedules a 10-second window starting 5 seconds from now |
    | `GET /admin/status` | Current shield state for all routes |
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

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/global_maintenance.py){ .md-button }

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

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/custom_responses.py){ .md-button }

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
    # Admin dashboard: http://localhost:8000/shield/   (admin / secret)
    ```

    **Full source:**

    ```python title="examples/fastapi/custom_responses.py"
    --8<-- "examples/fastapi/custom_responses.py"
    ```

---

### Webhooks

??? example "HTTP notifications on every state change"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/webhooks.py){ .md-button }

    Fully self-contained webhook demo: three receivers (generic JSON, Slack-formatted, and a custom payload) are mounted on the same app — no external service needed. Change a route state via the CLI or dashboard and watch the events appear at `/webhook-log`.

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
    # Admin:       http://localhost:8000/shield/       (admin / secret)
    ```

    **Trigger events:**

    ```bash
    shield config set-url http://localhost:8000/shield
    shield login admin                                   # password: secret
    shield disable GET:/payments --reason "hotfix"
    shield enable  GET:/payments
    shield maintenance GET:/orders --reason "stock sync"
    shield enable  GET:/orders
    ```

    Then open `http://localhost:8000/webhook-log` to see all three receivers fire for each state change.

    **Full source:**

    ```python title="examples/fastapi/webhooks.py"
    --8<-- "examples/fastapi/webhooks.py"
    ```

---

### Rate limiting

??? example "Per-IP, per-user, tiered limits, and custom 429 responses"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/rate_limiting.py){ .md-button }

    Demonstrates IP-based, user-based, and tiered rate limiting with a custom 429 response factory. Requires `api-shield[rate-limit]`.

    **Expected behavior:**

    | Endpoint | Limit | Key |
    |---|---|---|
    | `GET /public/posts` | 5/minute | IP |
    | `GET /users/me` | 20/minute | user |
    | `GET /reports` | free: 5/min, pro: 30/min | user + tier |
    | `GET /health` | unlimited | `@force_active` |

    **Run:**

    ```bash
    uv add "api-shield[all,rate-limit]"
    uv run uvicorn examples.fastapi.rate_limiting:app --reload
    # Admin dashboard:  http://localhost:8000/shield/   (admin / secret)
    # Rate limits tab:  http://localhost:8000/shield/rate-limits
    # Blocked log:      http://localhost:8000/shield/blocked
    ```

    **CLI quick-start:**

    ```bash
    shield login admin
    shield rl list
    shield rl set GET:/public/posts 20/minute   # raise limit live
    shield rl reset GET:/public/posts           # clear counters
    shield rl hits                              # blocked requests log
    ```

    **Full source:**

    ```python title="examples/fastapi/rate_limiting.py"
    --8<-- "examples/fastapi/rate_limiting.py"
    ```

---

### Custom backend (SQLite)

??? example "Implementing `ShieldBackend` with aiosqlite"

    [:material-github: View on GitHub](https://github.com/Attakay78/api-shield/blob/main/examples/fastapi/custom_backend/sqlite_backend.py){ .md-button }

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
    # Admin:       http://localhost:8000/shield/   (admin / secret)
    # Audit log:   http://localhost:8000/shield/audit
    ```

    **CLI quick-start:**

    ```bash
    shield config set-url http://localhost:8000/shield
    shield login admin          # password: secret
    shield status
    shield disable GET:/payments --reason "hotfix"
    shield enable  GET:/payments
    shield log
    ```

    **Full source:**

    ```python title="examples/fastapi/custom_backend/sqlite_backend.py"
    --8<-- "examples/fastapi/custom_backend/sqlite_backend.py"
    ```
