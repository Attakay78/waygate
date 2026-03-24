<div class="hero" markdown>
![api-shield](assets/logo-full.svg)

[:fontawesome-brands-github: View on GitHub](https://github.com/Attakay78/api-shield){ .md-button }

[![PyPI](https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi&cacheSeconds=300)](https://pypi.org/project/api-shield)
[![Python versions](https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B)](https://pypi.org/project/api-shield)
[![License](https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B)](https://github.com/Attakay78/api-shield/blob/main/LICENSE)
</div>

# api-shield

!!! warning "Early Access: your feedback shapes the roadmap"
    `api-shield` is fully functional and ready to use. We are actively building on a solid foundation and would love to hear from you. If you have feedback, feature ideas, or suggestions, **[open an issue on GitHub](https://github.com/Attakay78/api-shield/issues)**. Every voice helps make the library better for everyone.

**Feature flags and runtime control for Python APIs — rollouts, rate limits, manage maintenance windows across single ASGI services or a multi-service fleet without redeploying.**

Most "runtime control management" tools are blunt instruments: shut everything down or nothing at all. `api-shield` treats each route as a first-class entity with its own lifecycle. State changes take effect immediately, with no redeployment and no server restart.

---

## 30-second quickstart

> **FastAPI** is the currently supported adapter. Litestar, Starlette, Quart, and Django (ASGI) are on the roadmap. See [Adapters](adapters/index.md) for details.

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
GET /debug     → 200  in dev (default), 404 in production/staging
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

### Core (`shield.core`)

These features are framework-agnostic and available to every adapter.

| Feature | Description |
|---|---|
| ⚡ **Zero-restart control** | State changes are immediate, with no redeployment needed |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Shield never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates, no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status, new status |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler that activates and deactivates maintenance windows automatically |
| 🔔 **Webhooks** | Fire HTTP POST on every state change, with a built-in Slack formatter and support for custom formatters |
| 🚦 **Rate limiting** | Per-IP, per-user, per-API-key, or global counters with tiered limits, burst allowance, and runtime policy mutation |
| 🚩 **Feature flags** | Boolean, string, integer, float, and JSON flags with targeting rules, user segments, percentage rollouts, prerequisites, and a live evaluation stream — built on the OpenFeature standard |

### Framework adapters

#### FastAPI (`shield.fastapi`) — ✅ supported

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | `@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated`, `@rate_limit` — state lives next to the route |
| 📄 **OpenAPI integration** | Disabled and env-gated routes hidden from `/docs`; deprecated routes flagged; live maintenance banners in the Swagger UI |
| 🧩 **Dependency injection** | All decorators work as `Depends()` — enforce shield state per-handler without middleware |
| 🎨 **Custom responses** | Return HTML, redirects, or any response shape for blocked routes, per-route or as an app-wide default on the middleware |
| 🔀 **ShieldRouter** | Drop-in `APIRouter` replacement that auto-registers route metadata with the engine at startup |

---

## Decorators at a glance

| Decorator | Effect | HTTP response |
|---|---|---|
| `@maintenance(reason, start, end)` | Route temporarily unavailable | 503 + `Retry-After` |
| `@disabled(reason)` | Route permanently off | 503 |
| `@env_only("dev", "staging")` | Route restricted to named envs | 404 in other envs |
| `@deprecated(sunset, use_instead)` | Route still works, but headers warn clients | 200 + deprecation headers |
| `@force_active` | Route bypasses all shield checks | Always 200 |
| `@rate_limit("100/minute")` | Cap requests per IP, user, API key, or globally | 429 when exceeded |

---

## Framework support

### ASGI frameworks

api-shield is an **ASGI-native** library. The core (`shield.core`) is framework-agnostic with zero framework imports. Any ASGI framework can be supported — Starlette-based frameworks use `BaseHTTPMiddleware` directly; frameworks like Quart and Django that implement the ASGI spec independently use a raw ASGI callable adapter instead.

| Framework | Status | Adapter |
|---|---|---|
| **FastAPI** | ✅ Supported | `shield.fastapi` |
| **Litestar** | 🔜 Planned | — |
| **Starlette** | 🔜 Planned | — |
| **Quart** | 🔜 Planned | — |
| **Django (ASGI)** | 🔜 Planned | — |

### WSGI frameworks (Flask, Django, …)

!!! warning "WSGI support is out of scope for this project"
    `api-shield` is built on the ASGI standard. Adding WSGI support through shims or thread-bridging patches would require a persistent background event loop, a fundamentally different middleware model, and trade-offs that would compromise reliability for both ASGI and WSGI users.

    WSGI framework support (Flask, Django, Bottle, and others) will be delivered as a **separate, dedicated project** designed from the ground up for the synchronous request model. This keeps both projects clean, well-tested, and free of architectural compromises.

    [Open an issue](https://github.com/Attakay78/api-shield/issues) or watch this repo to be notified when the WSGI companion project launches.

---

## Next steps

- [**Tutorial: Installation**](tutorial/installation.md): get up and running in seconds
- [**Tutorial: First Decorator**](tutorial/first-decorator.md): put your first route in maintenance mode
- [**Tutorial: Rate Limiting**](tutorial/rate-limiting.md): per-IP, per-user, tiered limits, and more
- [**Tutorial: Feature Flags**](tutorial/feature-flags.md): targeting rules, segments, rollouts, and live events
- [**Reference: Decorators**](reference/decorators.md): full decorator API
- [**Reference: Rate Limiting**](reference/rate-limiting.md): `@rate_limit` parameters, models, and CLI commands
- [**Reference: ShieldEngine**](reference/engine.md): programmatic control
- [**Reference: Feature Flags**](reference/feature-flags.md): full flag/segment API, models, and CLI commands
- [**Reference: CLI**](reference/cli.md): all CLI commands
