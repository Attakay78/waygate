# FastAPI Adapter

The FastAPI adapter is the primary supported adapter. It provides middleware, decorators, a drop-in router, and OpenAPI integration.

---

## Installation

```bash
uv add "api-shield[fastapi]"        # adapter only
uv add "api-shield[all]"            # everything including CLI + admin
```

---

## Quick setup

```python
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
| `@maintenance(reason, start, end)` | `shield.fastapi.decorators` | 503 temporarily |
| `@disabled(reason)` | `shield.fastapi.decorators` | 503 permanently |
| `@env_only(*envs)` | `shield.fastapi.decorators` | 404 in other envs |
| `@deprecated(sunset, use_instead)` | `shield.fastapi.decorators` | 200 + headers |
| `@force_active` | `shield.fastapi.decorators` | Always 200 |

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

## Dependency injection

Shield decorators work as FastAPI `Depends()` dependencies for per-handler enforcement without middleware.

```python
from fastapi import Depends
from shield.fastapi.decorators import disabled, maintenance

# Pattern A: Decorator only (relies on ShieldMiddleware)
@router.get("/payments")
@maintenance(reason="DB migration")
async def get_payments():
    return {"payments": []}

# Pattern B: Depends() only (per-handler, no middleware required)
@router.get("/admin/report", dependencies=[Depends(disabled(reason="Use /v2/report"))])
async def admin_report():
    return {}

# Pattern C: Both (most explicit; works with or without middleware)
@router.get(
    "/orders",
    dependencies=[Depends(maintenance(reason="Order upgrade"))],
)
@maintenance(reason="Order upgrade")
async def get_orders():
    return {"orders": []}
```

| Pattern | Best for |
|---|---|
| Decorator only | Apps that always run `ShieldMiddleware` |
| `Depends()` only | Serverless / edge runtimes without middleware |
| Both | Library code or apps where callers may or may not use middleware |

---

## Using with FastAPI lifespan

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine:   # → backend.startup() … backend.shutdown()
        yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(ShieldMiddleware, engine=engine)
```

---

## Testing

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

| File | What it demonstrates |
|---|---|
| `examples/fastapi/basic.py` | Core decorators + `ShieldAdmin` |
| `examples/fastapi/dependency_injection.py` | `Depends()` pattern |
| `examples/fastapi/scheduled_maintenance.py` | Auto-activating maintenance windows |
| `examples/fastapi/global_maintenance.py` | Blocking every route at once |
| `examples/fastapi/custom_backend/sqlite_backend.py` | Full custom backend (SQLite) |

```bash
uv run uvicorn examples.fastapi.basic:app --reload
# Dashboard: http://localhost:8000/shield/  (login: admin / secret)
```
