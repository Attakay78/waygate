# Your first decorator

This tutorial shows you how to put a single route into maintenance mode and verify the behaviour. The examples use **FastAPI**.

## 1. Create a simple app

```python title="app.py"
from fastapi import FastAPI
from waygate import WaygateEngine
from waygate import MemoryBackend
from waygate.fastapi import WaygateMiddleware
from waygate.fastapi import WaygateRouter
from waygate.fastapi import maintenance, force_active

engine = WaygateEngine(backend=MemoryBackend())

app = FastAPI()
app.add_middleware(WaygateMiddleware, engine=engine)

router = WaygateRouter(engine=engine)

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
# /health → 200 (force_active bypasses all waygate checks)
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

1. `@maintenance(...)` stamps `__waygate_meta__ = {"status": "maintenance", "reason": "..."}` on the function. The function itself is **not modified**; it still runs normally if called directly.

2. When `app.include_router(router)` is called, `WaygateRouter` scans all routes for `__waygate_meta__` and calls `engine.register()` for each one.

3. On every HTTP request, `WaygateMiddleware` calls `engine.check(path)`. If the route is in maintenance, the engine raises `MaintenanceException` and the middleware returns a 503 response. The route handler never executes.

---

## Available decorators

| Decorator | Behaviour |
|---|---|
| `@maintenance(reason, start, end)` | 503, temporarily unavailable |
| `@disabled(reason)` | 503, permanently off |
| `@env_only("dev", "staging")` | 404 in other environments |
| `@deprecated(sunset, use_instead)` | 200 + deprecation headers |
| `@force_active` | Always 200, bypasses all checks |
| `@rate_limit("100/minute")` | 429 when the limit is exceeded; requires `waygate[rate-limit]` |

---

## Runtime changes without restart

Once the middleware is in place, you can change route state at runtime with no code changes and no restart:

```python
# Enable the route programmatically
await engine.enable("GET:/payments")

# Put it back in maintenance
await engine.set_maintenance("GET:/payments", reason="Second migration wave")
```

Or via the CLI (requires `WaygateAdmin` mounted; see [Admin Dashboard](admin-dashboard.md)):

```bash
waygate enable GET:/payments
waygate maintenance GET:/payments --reason "Second migration wave"
```

---

## Next step

[**Tutorial: Adding middleware →**](middleware.md)
