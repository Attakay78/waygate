# Your first decorator

This tutorial shows you how to put a single route into maintenance mode and verify the behaviour.

## 1. Create a simple FastAPI app

```python title="app.py"
from fastapi import FastAPI
from shield.core.engine import ShieldEngine
from shield.core.backends.memory import MemoryBackend
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.router import ShieldRouter
from shield.fastapi.decorators import maintenance, force_active

engine = ShieldEngine(backend=MemoryBackend())

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

router = ShieldRouter(engine=engine)

@router.get("/payments")
@maintenance(reason="Database migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@router.get("/health")
@force_active
async def health():
    return {"status": "ok"}

app.include_router(router)
```

## 2. Run it

```bash
uv run uvicorn app:app --reload
```

## 3. Verify the behaviour

```bash
# /payments → 503 Maintenance
curl -s http://localhost:8000/payments | python -m json.tool
```

```json
{
  "error": {
    "code": "MAINTENANCE_MODE",
    "message": "This endpoint is temporarily unavailable",
    "reason": "Database migration — back at 04:00 UTC",
    "path": "GET:/payments",
    "retry_after": null
  }
}
```

```bash
# /health → 200 (force_active bypasses all shield checks)
curl -s http://localhost:8000/health
```

```json
{"status": "ok"}
```

---

## How it works

```
@router.get("/payments")
@maintenance(reason="Database migration")
async def get_payments():
    ...
```

1. `@maintenance(...)` stamps `__shield_meta__ = {"status": "maintenance", "reason": "..."}` on the function. The function itself is **not modified** — it still runs normally if called directly.

2. When `app.include_router(router)` is called, `ShieldRouter` scans all routes for `__shield_meta__` and calls `engine.register()` for each one.

3. On every HTTP request, `ShieldMiddleware` calls `engine.check(path)`. If the route is in maintenance, the engine raises `MaintenanceException` and the middleware returns a 503 response — the route handler never executes.

---

## Available decorators

| Decorator | Behaviour |
|---|---|
| `@maintenance(reason, start, end)` | 503 — temporarily unavailable |
| `@disabled(reason)` | 503 — permanently off |
| `@env_only("dev", "staging")` | 404 in other environments |
| `@deprecated(sunset, use_instead)` | 200 + deprecation headers |
| `@force_active` | Always 200 — bypasses all checks |

---

## Runtime changes without restart

Once the middleware is in place, you can change route state at runtime — no code changes, no restart:

```python
# Enable the route programmatically
await engine.enable("GET:/payments")

# Put it back in maintenance
await engine.set_maintenance("GET:/payments", reason="Second migration wave")
```

Or via the CLI (requires `ShieldAdmin` mounted — see [Admin Dashboard](admin-dashboard.md)):

```bash
shield enable GET:/payments
shield maintenance GET:/payments --reason "Second migration wave"
```

---

## Next step

[**Tutorial: Adding middleware →**](middleware.md)
