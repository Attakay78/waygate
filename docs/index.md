<div class="hero" markdown>
![api-shield](assets/logo-full.svg)

[:fontawesome-brands-github: View on GitHub](https://github.com/Attakay78/api-shield){ .md-button }

[![PyPI](https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi)](https://pypi.org/project/api-shield)
[![Python versions](https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B)](https://pypi.org/project/api-shield)
[![License](https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B)](https://github.com/Attakay78/api-shield/blob/main/LICENSE)
</div>

# api-shield

**Route lifecycle management for Python web frameworks — maintenance mode, environment gating, deprecation, admin panels, and more. No restarts required.**

Most "maintenance mode" tools are blunt instruments: shut everything down or nothing at all. `api-shield` treats each route as a first-class entity with its own lifecycle. State changes take effect immediately through middleware — no redeployment, no server restart.

---

## 30-second quickstart

```bash
uv add "api-shield[all]"
```

```python
from fastapi import FastAPI
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware, ShieldAdmin,
    apply_shield_to_openapi,
    maintenance, env_only, disabled, force_active, deprecated,
)

engine = make_engine()  # reads SHIELD_BACKEND, SHIELD_ENV from env

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="Database migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active          # always 200, immune to all shield checks
async def health():
    return {"status": "ok"}

@app.get("/debug")
@env_only("dev", "staging")   # silent 404 in production
async def debug():
    return {"debug": True}

@app.get("/v1/users")
@deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/users")
async def v1_users():
    return {"users": []}

apply_shield_to_openapi(app, engine)

# Mount the admin dashboard + REST API (for the CLI) at /shield
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

That's it. Routes respond immediately:

```
GET /payments  → 503  {"error": {"code": "MAINTENANCE_MODE", ...}}
GET /health    → 200  always
GET /debug     → 404  in production, 200 in dev/staging
GET /v1/users  → 200  + Deprecation / Sunset / Link headers
```

And you can manage them from the CLI without touching code:

```bash
shield config set-url http://localhost:8000/shield
shield login admin
shield status
shield enable GET:/payments
```

---

## Key features

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | Route state lives next to the route definition, not in a separate config file |
| ⚡ **Zero-restart control** | State changes are immediate — no redeployment needed |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Shield never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates — no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines |
| 📄 **OpenAPI integration** | Disabled / env-gated routes hidden from `/docs`; deprecated routes flagged automatically |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status → new status |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler — maintenance windows activate and deactivate automatically |

---

## Decorators at a glance

| Decorator | Effect | HTTP response |
|---|---|---|
| `@maintenance(reason, start, end)` | Route temporarily unavailable | 503 + `Retry-After` |
| `@disabled(reason)` | Route permanently off | 503 |
| `@env_only("dev", "staging")` | Route restricted to named envs | 404 in other envs |
| `@deprecated(sunset, use_instead)` | Route still works, but headers warn clients | 200 + deprecation headers |
| `@force_active` | Route bypasses all shield checks | Always 200 |

---

## Next steps

- [**Tutorial: Installation**](tutorial/installation.md) — get up and running in 5 minutes
- [**Tutorial: First Decorator**](tutorial/first-decorator.md) — put your first route in maintenance mode
- [**Reference: Decorators**](reference/decorators.md) — full decorator API
- [**Reference: ShieldEngine**](reference/engine.md) — programmatic control
- [**Reference: CLI**](reference/cli.md) — all CLI commands
