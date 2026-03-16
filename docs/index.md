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

**Route(API) lifecycle management for Python web frameworks: maintenance mode, environment gating, deprecation, rate limiting, admin panels, and more. No restarts required.**

Most "route lifecycle management" tools are blunt instruments: shut everything down or nothing at all. `api-shield` treats each route as a first-class entity with its own lifecycle. State changes take effect immediately through middleware, with no redeployment and no server restart.

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

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | Route state lives next to the route definition, not in a separate config file |
| ⚡ **Zero-restart control** | State changes are immediate, with no redeployment needed |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Shield never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates, no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines |
| 📄 **OpenAPI integration** | Disabled and env-gated routes hidden from `/docs`; deprecated routes flagged automatically |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status, new status |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler that activates and deactivates maintenance windows automatically |
| 🔔 **Webhooks** | Fire HTTP POST on every state change, with a built-in Slack formatter and support for custom formatters |
| 🎨 **Custom responses** | Return HTML, redirects, or any response shape for blocked routes, per-route or as an app-wide default |
| 🚦 **Rate limiting** | Per-IP, per-user, per-API-key, or global counters with tiered limits, burst allowance, and runtime policy mutation |

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

## Next steps

- [**Tutorial: Installation**](tutorial/installation.md): get up and running in seconds
- [**Tutorial: First Decorator**](tutorial/first-decorator.md): put your first route in maintenance mode
- [**Tutorial: Rate Limiting**](tutorial/rate-limiting.md): per-IP, per-user, tiered limits, and more
- [**Reference: Decorators**](reference/decorators.md): full decorator API
- [**Reference: Rate Limiting**](reference/rate-limiting.md): `@rate_limit` parameters, models, and CLI commands
- [**Reference: ShieldEngine**](reference/engine.md): programmatic control
- [**Reference: CLI**](reference/cli.md): all CLI commands
