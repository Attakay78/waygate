<div align="center">
  <img src="https://raw.githubusercontent.com/Attakay78/switchly/main/docs/assets/logo-full.svg" alt="Switchly" width="600"/>

  <p><strong>Switchly gives you runtime control of your APIs to toggle features, schedule maintenance, enforce rate limits, and perform rollouts without redeploying.</strong></p>

  <a href="https://pypi.org/project/switchly"><img src="https://img.shields.io/pypi/v/switchly?color=F59E0B&label=pypi&cacheSeconds=300" alt="PyPI"></a>
  <a href="https://pypi.org/project/switchly"><img src="https://img.shields.io/pypi/pyversions/switchly?color=F59E0B" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Attakay78/switchly?color=F59E0B" alt="License"></a>
</div>

---

> [!WARNING]
> **Early Access:** `switchly` is fully functional and ready to use. We're actively building on it and real-world feedback is invaluable. If you have feedback, feature ideas, or suggestions, [open an issue](https://github.com/Attakay78/switchly/issues).

---

## Key features

### Core (`switchly.core`)

These features are framework-agnostic and available to any adapter.

| Feature | Description |
|---|---|
| 🚩 **Feature flags** | Boolean, string, integer, float, and JSON flags with targeting rules, user segments, percentage rollouts, prerequisites, and a live evaluation stream. Built on the [OpenFeature](https://openfeature.dev/) standard |
| 🚦 **Rate limiting** | Per-IP, per-user, per-API-key, or global counters with tiered limits, burst allowance, and runtime mutation |
| ⏰ **Scheduled windows** | `asyncio`-native scheduler, maintenance windows activate and deactivate automatically |
| 🔔 **Webhooks** | Fire HTTP POST on every state change. Built-in Slack formatter and custom formatters supported |
| 📋 **Audit log** | Every state change is recorded: who, when, what route, old status → new status |
| 🖥️ **Admin dashboard** | HTMX-powered UI with live SSE updates, no JS framework required |
| 🖱️ **REST API + CLI** | Full programmatic control from the terminal or CI pipelines, works over HTTPS remotely |
| 🏗️ **Switchly Server** | Centralised control plane for multi-service architectures. SDK clients sync state via SSE with zero per-request latency |
| 🌐 **Multi-service CLI** | `SWITCHLY_SERVICE` env var scopes every command; `switchly services` lists connected services |
| ⚡ **Zero-restart control** | State changes take effect immediately, no redeployment or server restart needed |
| 🔄 **Sync & async** | Full support for both `async def` and plain `def` route handlers. Use `await engine.*` or `engine.sync.*` |
| 🛡️ **Fail-open by default** | If the backend is unreachable, requests pass through. Switchly never takes down your API |
| 🔌 **Pluggable backends** | In-memory (default), file-based JSON, or Redis for multi-instance deployments |

### Framework adapters

#### FastAPI (`switchly.fastapi`) ✅ supported

| Feature | Description |
|---|---|
| 🎨 **Decorator-first DX** | `@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated`, `@rate_limit`. State lives next to the route |
| 📄 **OpenAPI integration** | Disabled / env-gated routes hidden from `/docs`; deprecated routes flagged; live maintenance banners in the Swagger UI |
| 🧩 **Dependency injection** | All decorators work as `Depends()`, enforcing route state per-handler without middleware |
| 🎨 **Custom responses** | Return HTML, redirects, or any response shape for blocked routes. Set per-route or as an app-wide default on the middleware |
| 🔀 **SwitchlyRouter** | Drop-in `APIRouter` replacement that auto-registers route metadata with the engine at startup |

<div align="center">
  <img src="https://raw.githubusercontent.com/Attakay78/switchly/main/docs/assets/openapi.png" alt="Switchly OpenAPI integration" width="48%"/>
  <img src="https://raw.githubusercontent.com/Attakay78/switchly/main/docs/assets/openapi-maintenance.png" alt="Switchly maintenance banner in Swagger UI" width="48%"/>
  <p><em>Disabled and env-gated routes hidden from /docs. Maintenance banners injected live.</em></p>
</div>

---

## Install

```bash
uv add "switchly[all]"
# or: pip install "switchly[all]"
```

## Quickstart

> We currently support **FastAPI**. More framework adapters are on the way.

```python
from fastapi import FastAPI
from switchly import make_engine
from switchly.fastapi import (
    SwitchlyMiddleware, SwitchlyAdmin, apply_switchly_to_openapi,
    maintenance, env_only, disabled, force_active, deprecated,
)

engine = make_engine()

app = FastAPI()
app.add_middleware(SwitchlyMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active
async def health():
    return {"status": "ok"}

apply_switchly_to_openapi(app, engine)
app.mount("/switchly", SwitchlyAdmin(engine=engine, auth=("admin", "secret")))
```

```
GET /payments  → 503  {"error": {"code": "MAINTENANCE_MODE", ...}}
GET /health    → 200  always
```

Manage routes from the CLI with no code changes or restarts:

```bash
switchly config set-url http://localhost:8000/switchly
switchly login admin
switchly status
switchly enable GET:/payments
switchly global enable --reason "Deploying v2" --exempt /health
```

<div align="center">
  <img src="https://raw.githubusercontent.com/Attakay78/switchly/main/docs/assets/dashboard.png" alt="Switchly admin dashboard" width="90%"/>
  <p><em>Admin dashboard — route states, audit log, rate limits, and feature flags. No JS framework required.</em></p>
</div>

## Decorators

| Decorator | Effect | Status |
|---|---|---|
| `@maintenance(reason, start, end)` | Temporarily unavailable | 503 |
| `@disabled(reason)` | Permanently off | 503 |
| `@env_only("dev", "staging")` | Restricted to named environments | 404 elsewhere |
| `@deprecated(sunset, use_instead)` | Still works, injects deprecation headers | 200 |
| `@force_active` | Bypasses all switchly checks | Always 200 |
| `@rate_limit("100/minute")` | Cap requests per IP, user, API key, or globally | 429 |
### Custom responses (FastAPI)

By default, blocked routes return a structured JSON error body. You can replace it with HTML, a redirect, plain text, or custom JSON in two ways:

**Per-route:** pass `response=` directly on the decorator:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from switchly.fastapi import maintenance, disabled

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

**Global default:** set once on `SwitchlyMiddleware`, applies to every route without a per-route factory:

```python
app.add_middleware(
    SwitchlyMiddleware,
    engine=engine,
    responses={
        "maintenance": maintenance_page,   # all maintenance routes
        "disabled": lambda req, exc: HTMLResponse(
            f"<h1>Gone</h1><p>{exc.reason}</p>", status_code=503
        ),
    },
)
```

Resolution order: **per-route `response=`** → **global `responses[...]`** → **built-in JSON**. The factory can be sync or async and receives the live `Request` and the `SwitchlyException` that triggered the block.

## Rate limiting

```python
from switchly.fastapi import rate_limit

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

Policies can be mutated at runtime without redeploying (`switchly rl` and `switchly rate-limits` are aliases):

```bash
switchly rl set GET:/public/posts 20/minute   # raise the limit live
switchly rl reset GET:/public/posts           # clear counters
switchly rl hits                              # blocked requests log
```

Requires `switchly[rate-limit]`. Powered by [limits](https://limits.readthedocs.io/en/stable/).

---

## Feature flags

switchly ships a full feature flag system built on the [OpenFeature](https://openfeature.dev/) standard. All five flag types, multi-condition targeting rules, user segments, percentage rollouts, and a live evaluation stream. Managed from the dashboard or CLI with no code changes.

```python
from switchly import (
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
switchly flags list
switchly flags eval new-checkout --user user_123
switchly flags disable new-checkout          # kill-switch
switchly flags enable new-checkout
switchly flags stream                        # live evaluation events

switchly segments create beta_users --name "Beta Users"
switchly segments include beta_users --context-key user_123,user_456
switchly segments add-rule beta_users --attribute plan --operator in --values pro,enterprise
```

Requires `switchly[flags]`.

---

## Framework support

switchly's core is completely framework-agnostic with zero framework imports. Adapters plug into the engine and expose framework-native patterns like decorators, middleware, and routers.

| Framework | Status | Adapter |
|---|---|---|
| **FastAPI** | ✅ Supported | `switchly.fastapi` |
| More coming | 🔜 On the way | — |

> Want your framework supported? [Open an issue](https://github.com/Attakay78/switchly/issues).

---

## Backends

### Embedded mode (single service)

| Backend | Persistence | Multi-instance | Best for |
|---|---|---|---|
| `MemoryBackend` | No | No | Development, tests |
| `FileBackend` | Yes | No (single process) | Simple single-instance prod |
| `RedisBackend` | Yes | Yes | Load-balanced / multi-worker prod |

For rate limiting in multi-worker deployments, use `RedisBackend`. Counters are atomic and shared across all processes.

### Switchly Server mode (multi-service)

Run a dedicated `SwitchlyServer` process and connect each service via `SwitchlySDK`. State is managed centrally; enforcement happens locally with zero per-request network overhead.

```python
# Switchly Server (centralised, runs once)
from switchly.server import SwitchlyServer
switchly_app = SwitchlyServer(backend=MemoryBackend(), auth=("admin", "secret"))

# Each service (connects to the Switchly Server)
from switchly.sdk import SwitchlySDK
sdk = SwitchlySDK(server_url="http://switchly-server:9000", app_id="payments-service")
sdk.attach(app)
```

| Scenario | Switchly Server backend | SDK `rate_limit_backend` |
|---|---|---|
| Multi-service, single replica each | `MemoryBackend` or `FileBackend` | not needed |
| Multi-service, multiple replicas | `RedisBackend` | `RedisBackend` (shared counters) |

---

## Documentation

Full documentation at **[attakay78.github.io/switchly](https://attakay78.github.io/switchly)**

| | |
|---|---|
| [Tutorial](https://attakay78.github.io/switchly/tutorial/installation/) | Get started in 5 minutes |
| [Decorators reference](https://attakay78.github.io/switchly/reference/decorators/) | All decorator options |
| [Rate limiting](https://attakay78.github.io/switchly/tutorial/rate-limiting/) | Per-IP, per-user, tiered limits |
| [Feature flags](https://attakay78.github.io/switchly/tutorial/feature-flags/) | Targeting rules, segments, rollouts, live events |
| [SwitchlyEngine reference](https://attakay78.github.io/switchly/reference/engine/) | Programmatic control |
| [Backends](https://attakay78.github.io/switchly/tutorial/backends/) | Memory, File, Redis, Switchly Server, custom |
| [Admin dashboard](https://attakay78.github.io/switchly/tutorial/admin-dashboard/) | Mounting SwitchlyAdmin |
| [CLI reference](https://attakay78.github.io/switchly/reference/cli/) | All CLI commands |
| [Switchly Server guide](https://attakay78.github.io/switchly/guides/switchly-server/) | Multi-service centralized control |
| [Distributed deployments](https://attakay78.github.io/switchly/guides/distributed/) | Multi-instance backend guide |
| [Production guide](https://attakay78.github.io/switchly/guides/production/) | Monitoring & deployment automation |

## License

[MIT](LICENSE)
