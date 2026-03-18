<div align="center">
  <img src="api-shield-logo.svg" alt="API Shield" width="600"/>

  <p><strong>Route(API) lifecycle management for Python web frameworks — maintenance mode, environment gating, deprecation, rate limiting, admin panels, and more. No restarts required.</strong></p>

  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi&cacheSeconds=300" alt="PyPI"></a>
  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B" alt="License"></a>
</div>

---

> [!WARNING]
> **Early Access** — `api-shield` is fully functional and ready to use. We are actively building on top of a solid foundation and your real-world experience is invaluable at this stage. If you have feedback, feature ideas, or suggestions, please [open an issue](https://github.com/Attakay78/api-shield/issues) — every voice helps shape the roadmap.

---

## Key features

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | Route state lives next to the route definition, not in a separate config file |
| ⚡ **Zero-restart control** | State changes take effect immediately — no redeployment or server restart needed |
| 🔄 **Sync & async** | Full support for both `async def` and plain `def` route handlers — use `await engine.*` or `engine.sync.*` |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Shield never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates — no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines — works over HTTPS remotely |
| 📄 **OpenAPI integration** | Disabled / env-gated routes hidden from `/docs`; deprecated routes flagged automatically |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status → new status |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler — maintenance windows activate and deactivate automatically |
| 🔔 **Webhooks** | Fire HTTP POST on every state change — built-in Slack formatter and custom formatters supported |
| 🎨 **Custom responses** | Return HTML, redirects, or any response shape for blocked routes — per-route or app-wide default |
| 🚦 **Rate limiting** | Per-IP, per-user, per-API-key, or global counters — tiered limits, burst allowance, runtime mutation |

---

## Install

```bash
uv add "api-shield[all]"
# or: pip install "api-shield[all]"
```

## Quickstart

```python
from fastapi import FastAPI
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware, ShieldAdmin, apply_shield_to_openapi,
    maintenance, env_only, disabled, force_active, deprecated,
)

engine = make_engine()

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active
async def health():
    return {"status": "ok"}

apply_shield_to_openapi(app, engine)
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

```
GET /payments  → 503  {"error": {"code": "MAINTENANCE_MODE", ...}}
GET /health    → 200  always
```

Manage routes from the CLI — no code changes, no restarts:

```bash
shield config set-url http://localhost:8000/shield
shield login admin
shield status
shield enable GET:/payments
shield global enable --reason "Deploying v2" --exempt /health
```

## Decorators

| Decorator | Effect | Status |
|---|---|---|
| `@maintenance(reason, start, end)` | Temporarily unavailable | 503 |
| `@disabled(reason)` | Permanently off | 503 |
| `@env_only("dev", "staging")` | Restricted to named environments | 404 elsewhere |
| `@deprecated(sunset, use_instead)` | Still works, injects deprecation headers | 200 |
| `@force_active` | Bypasses all shield checks | Always 200 |
| `@rate_limit("100/minute")` | Cap requests per IP, user, API key, or globally | 429 |
### Custom responses

By default, blocked routes return a structured JSON error body. You can replace it with anything — HTML, a redirect, plain text, or your own JSON — in two ways:

**Per-route** — pass `response=` directly on the decorator:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from shield.fastapi import maintenance, disabled

def maintenance_page(request: Request, exc: Exception) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Down for maintenance</h1><p>{exc.reason}</p>", status_code=503
    )

@router.get("/payments")
@maintenance(reason="DB migration", response=maintenance_page)
async def payments():
    return {"payments": []}

@router.get("/orders")
@maintenance(reason="Upgrade in progress", response=lambda *_: RedirectResponse("/status"))
async def orders():
    return {"orders": []}
```

**Global default** — set once on `ShieldMiddleware`, applies to every route without a per-route factory:

```python
app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "maintenance": maintenance_page,   # all maintenance routes
        "disabled": lambda req, exc: HTMLResponse(
            f"<h1>Gone</h1><p>{exc.reason}</p>", status_code=503
        ),
    },
)
```

Resolution order: **per-route `response=`** → **global `responses[...]`** → **built-in JSON**. The factory can be sync or async and receives the live `Request` and the `ShieldException` that triggered the block.

## Rate limiting

```python
from shield.fastapi.decorators import rate_limit

@router.get("/public/posts")
@rate_limit("10/minute")               # 10 req/min per IP
async def list_posts():
    return {"posts": [...]}

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

Policies can be mutated at runtime without redeploying (`shield rl` and `shield rate-limits` are aliases):

```bash
shield rl set GET:/public/posts 20/minute   # raise the limit live
shield rl reset GET:/public/posts           # clear counters
shield rl hits                              # blocked requests log
```

Requires `api-shield[rate-limit]`. Powered by [limits](https://limits.readthedocs.io/en/stable/).

---

## Backends

| Backend | Persistence | Multi-instance |
|---|---|---|
| `MemoryBackend` | No | No |
| `FileBackend` | Yes | No |
| `RedisBackend` | Yes | Yes |

For rate limiting in multi-worker deployments, use `RedisBackend` — counters are atomic and shared across all processes.

---

## Documentation

Full documentation at **[attakay78.github.io/api-shield](https://attakay78.github.io/api-shield)**

| | |
|---|---|
| [Tutorial](https://attakay78.github.io/api-shield/tutorial/installation/) | Get started in 5 minutes |
| [Decorators reference](https://attakay78.github.io/api-shield/reference/decorators/) | All decorator options |
| [Rate limiting](https://attakay78.github.io/api-shield/tutorial/rate-limiting/) | Per-IP, per-user, tiered limits |
| [ShieldEngine reference](https://attakay78.github.io/api-shield/reference/engine/) | Programmatic control |
| [Backends](https://attakay78.github.io/api-shield/tutorial/backends/) | Memory, File, Redis, custom |
| [Admin dashboard](https://attakay78.github.io/api-shield/tutorial/admin-dashboard/) | Mounting ShieldAdmin |
| [CLI reference](https://attakay78.github.io/api-shield/reference/cli/) | All CLI commands |
| [Production guide](https://attakay78.github.io/api-shield/guides/production/) | Monitoring & deployment automation |

## License

[MIT](LICENSE)
