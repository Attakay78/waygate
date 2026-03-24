<div align="center">
  <img src="api-shield-logo.svg" alt="API Shield" width="600"/>

  <p><strong>Feature flags and runtime control for Python APIs — rollouts, rate limits, manage maintenance windows across single ASGI services or a multi-service fleet without redeploying.</strong></p>

  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi&cacheSeconds=300" alt="PyPI"></a>
  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B" alt="License"></a>
</div>

---

> [!WARNING]
> **Early Access** — `api-shield` is fully functional and ready to use. We are actively building on top of a solid foundation and your real-world experience is invaluable at this stage. If you have feedback, feature ideas, or suggestions, please [open an issue](https://github.com/Attakay78/api-shield/issues) — every voice helps shape the roadmap.

---

## Key features

### Core (`shield.core`)

These features are framework-agnostic and available to any adapter.

| Feature | Description |
|---|---|
| ⚡ **Zero-restart control** | State changes take effect immediately — no redeployment or server restart needed |
| 🔄 **Sync & async** | Full support for both `async def` and plain `def` route handlers — use `await engine.*` or `engine.sync.*` |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Shield never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates — no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines — works over HTTPS remotely |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status → new status |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler — maintenance windows activate and deactivate automatically |
| 🔔 **Webhooks** | Fire HTTP POST on every state change — built-in Slack formatter and custom formatters supported |
| 🚦 **Rate limiting** | Per-IP, per-user, per-API-key, or global counters — tiered limits, burst allowance, runtime mutation |
| 🚩 **Feature flags** | Boolean, string, integer, float, and JSON flags — targeting rules, user segments, percentage rollouts, prerequisites, and a live evaluation stream. Built on the [OpenFeature](https://openfeature.dev/) standard |
| 🏗️ **Shield Server** | Centralised control plane for multi-service architectures — SDK clients sync state via SSE with zero per-request latency |
| 🌐 **Multi-service CLI** | `SHIELD_SERVICE` env var scopes every command; `shield services` lists connected services |

### Framework adapters

#### FastAPI (`shield.fastapi`) — ✅ supported

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | `@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated`, `@rate_limit` — state lives next to the route |
| 📄 **OpenAPI integration** | Disabled / env-gated routes hidden from `/docs`; deprecated routes flagged; live maintenance banners in the Swagger UI |
| 🧩 **Dependency injection** | All decorators work as `Depends()` — enforce shield state per-handler without middleware |
| 🎨 **Custom responses** | Return HTML, redirects, or any response shape for blocked routes — per-route or app-wide default on the middleware |
| 🔀 **ShieldRouter** | Drop-in `APIRouter` replacement that auto-registers route metadata with the engine at startup |

---

## Install

```bash
uv add "api-shield[all]"
# or: pip install "api-shield[all]"
```

## Quickstart

> FastAPI is the currently supported adapter. Litestar, Starlette, Quart, and Django (ASGI) are on the roadmap.

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
### Custom responses (FastAPI)

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

## Feature flags

api-shield ships a full feature flag system built on the [OpenFeature](https://openfeature.dev/) standard. All five flag types, multi-condition targeting rules, user segments, percentage rollouts, and a live evaluation stream — managed from the dashboard or CLI with no code changes.

```python
from shield.core.feature_flags.models import (
    FeatureFlag, FlagType, FlagVariation, RolloutVariation,
    TargetingRule, RuleClause, Operator, EvaluationContext,
)

engine.use_openfeature()

# Define a boolean flag with a 20% rollout and individual targeting
await engine.save_flag(
    FeatureFlag(
        key="new-checkout",
        name="New Checkout Flow",
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on",  value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough=[
            RolloutVariation(variation="on",  weight=20_000),  # 20%
            RolloutVariation(variation="off", weight=80_000),  # 80%
        ],
        targets={"on": ["beta_tester_1"]},   # individual targeting
        rules=[
            TargetingRule(
                description="Enterprise users always get the new flow",
                clauses=[RuleClause(attribute="plan", operator=Operator.IS, values=["enterprise"])],
                variation="on",
            )
        ],
    )
)

# Evaluate in an async route handler
ctx = EvaluationContext(key=user_id, attributes={"plan": user.plan})
enabled = await engine.flag_client.get_boolean_value("new-checkout", False, ctx)

# Evaluate in a sync def handler (thread-safe)
enabled = engine.sync.flag_client.get_boolean_value("new-checkout", False, {"targeting_key": user_id})
```

Manage flags and segments from the CLI:

```bash
shield flags list
shield flags eval new-checkout --user user_123
shield flags disable new-checkout          # kill-switch
shield flags enable new-checkout
shield flags stream                        # live evaluation events

shield segments create beta_users --name "Beta Users"
shield segments include beta_users --context-key user_123,user_456
shield segments add-rule beta_users --attribute plan --operator in --values pro,enterprise
```

Requires `api-shield[flags]`.

---

## Framework support

api-shield is built on the **ASGI** standard. The core (`shield.core`) is completely framework-agnostic and has zero framework imports. Any ASGI framework can be supported — either via a Starlette `BaseHTTPMiddleware` (for Starlette-based frameworks) or a raw ASGI callable for frameworks like Quart and Django that implement the ASGI spec independently.

### ASGI frameworks

| Framework | Status | Adapter |
|---|---|---|
| **FastAPI** | ✅ Supported | `shield.fastapi` |
| **Litestar** | 🔜 Planned | — |
| **Starlette** | 🔜 Planned | — |
| **Quart** | 🔜 Planned | — |
| **Django (ASGI)** | 🔜 Planned | — |

> Want support for another ASGI framework? [Open an issue](https://github.com/Attakay78/api-shield/issues).

### WSGI frameworks (Flask, Django, …)

> [!IMPORTANT]
> **WSGI support is out of scope for this project.**
>
> `api-shield` is an ASGI-native library. Bolting WSGI support in through shims or patches would require a persistent background event loop, thread-bridging hacks, and a fundamentally different middleware model — complexity that would compromise the quality and reliability of both layers.
>
> WSGI framework support (Flask, Django, Bottle, …) will be delivered as a **separate, dedicated project** designed from the ground up for the synchronous request model. This keeps both projects clean, well-tested, and maintainable without trade-offs.
>
> Watch this repo or [open an issue](https://github.com/Attakay78/api-shield/issues) to be notified when the WSGI companion project launches.

---

## Backends

### Embedded mode (single service)

| Backend | Persistence | Multi-instance | Best for |
|---|---|---|---|
| `MemoryBackend` | No | No | Development, tests |
| `FileBackend` | Yes | No (single process) | Simple single-instance prod |
| `RedisBackend` | Yes | Yes | Load-balanced / multi-worker prod |

For rate limiting in multi-worker deployments, use `RedisBackend` — counters are atomic and shared across all processes.

### Shield Server mode (multi-service)

Run a dedicated `ShieldServer` process and connect each service via `ShieldSDK`. State is managed centrally; enforcement happens locally with zero per-request network overhead.

```python
# Shield Server (centralised — runs once)
from shield.server import ShieldServer
shield_app = ShieldServer(backend=MemoryBackend(), auth=("admin", "secret"))

# Each service (connects to the Shield Server)
from shield.sdk import ShieldSDK
sdk = ShieldSDK(server_url="http://shield-server:9000", app_id="payments-service")
sdk.attach(app)
```

| Scenario | Shield Server backend | SDK `rate_limit_backend` |
|---|---|---|
| Multi-service, single replica each | `MemoryBackend` or `FileBackend` | not needed |
| Multi-service, multiple replicas | `RedisBackend` | `RedisBackend` (shared counters) |

---

## Documentation

Full documentation at **[attakay78.github.io/api-shield](https://attakay78.github.io/api-shield)**

| | |
|---|---|
| [Tutorial](https://attakay78.github.io/api-shield/tutorial/installation/) | Get started in 5 minutes |
| [Decorators reference](https://attakay78.github.io/api-shield/reference/decorators/) | All decorator options |
| [Rate limiting](https://attakay78.github.io/api-shield/tutorial/rate-limiting/) | Per-IP, per-user, tiered limits |
| [Feature flags](https://attakay78.github.io/api-shield/tutorial/feature-flags/) | Targeting rules, segments, rollouts, live events |
| [ShieldEngine reference](https://attakay78.github.io/api-shield/reference/engine/) | Programmatic control |
| [Backends](https://attakay78.github.io/api-shield/tutorial/backends/) | Memory, File, Redis, Shield Server, custom |
| [Admin dashboard](https://attakay78.github.io/api-shield/tutorial/admin-dashboard/) | Mounting ShieldAdmin |
| [CLI reference](https://attakay78.github.io/api-shield/reference/cli/) | All CLI commands |
| [Shield Server guide](https://attakay78.github.io/api-shield/guides/shield-server/) | Multi-service centralized control |
| [Distributed deployments](https://attakay78.github.io/api-shield/guides/distributed/) | Multi-instance backend guide |
| [Production guide](https://attakay78.github.io/api-shield/guides/production/) | Monitoring & deployment automation |

## License

[MIT](LICENSE)
