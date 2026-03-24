# Rate Limiting

Rate limiting protects your API from overuse and abuse by capping how many requests a client can make in a given time window. In api-shield, rate limiting is declared with a single decorator and enforced by the same middleware that handles maintenance mode and deprecation.

---

## Installation

Rate limiting is powered by the [limits](https://limits.readthedocs.io/en/stable/) library, which handles all counter arithmetic, window management, and storage backends. Install it via the `rate-limit` extra:

```bash
uv add "api-shield[rate-limit]"
# or: pip install "api-shield[rate-limit]"
```

---

## Basic usage

```python
from shield.fastapi.decorators import rate_limit

@router.get("/public/posts")
@rate_limit("10/minute")
async def list_posts():
    return {"posts": [...]}
```

That is all. Every IP address can now call this endpoint at most 10 times per minute. Requests beyond that receive a `429 Too Many Requests` response with `Retry-After` and `X-RateLimit-*` headers.

---

## Response headers

Every request to a rate-limited route gets these headers:

```http
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 7
X-RateLimit-Reset: 1748736120
```

Blocked requests additionally receive:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 23
```

The `429` response body:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Too many requests",
    "limit": "10/minute",
    "retry_after": 23
  }
}
```

---

## Limit string format

Limits follow the format `<count>/<period>`:

```
"10/minute"
"100/hour"
"1000/day"
"5/second"
"50/minute"
```

---

## Key strategies

The key strategy controls which bucket a request counts against. By default, each IP address has its own independent counter.

### IP (default)

One counter per IP address. Works with no configuration.

```python
@rate_limit("100/minute")               # same as key="ip"
@rate_limit("100/minute", key="ip")
```

IP is read from `X-Forwarded-For`, `X-Real-IP`, or the ASGI scope, in that order.

---

### Per user

One counter per authenticated user, identified via `request.state.user_id`.

```python
@rate_limit("100/minute", key="user")
```

Your auth middleware must set `request.state.user_id` before the rate limiter runs. When the attribute is absent, the default behaviour is `EXEMPT` (the request passes through without consuming quota).

```python
# Reject unauthenticated callers with 429 instead of exempting them
@rate_limit("100/minute", key="user", on_missing_key="block")

# Fall back to IP-based counting when user_id is absent
@rate_limit("100/minute", key="user", on_missing_key="fallback_ip")
```

---

### Per API key

One counter per `X-API-Key` header value.

```python
@rate_limit("50/minute", key="api_key")
```

When the header is absent, the default behaviour is `FALLBACK_IP`: the request is still rate limited, but bucketed by IP rather than by key.

---

### Global (shared counter)

All callers share one counter for the route. Use this to cap aggregate throughput regardless of who is calling — useful for expensive endpoints.

```python
@rate_limit("5/minute", key="global")
```

---

### Custom key function

Provide a sync or async callable that returns the key string for each request.

```python
# sync
def tenant_key(request: Request) -> str | None:
    return request.headers.get("X-Tenant-ID")

@rate_limit("500/minute", key=tenant_key)
```

```python
# async — useful when key extraction requires an await (e.g. cache lookup)
async def tenant_key(request: Request) -> str | None:
    return await cache.get(request.headers.get("X-Tenant-ID"))

@rate_limit("500/minute", key=tenant_key)
```

Return `None` when no key can be extracted; `on_missing_key` controls what happens next.

---

## Algorithms

All algorithms are implemented by the `limits` library. api-shield selects which one to use.

| Algorithm | Behaviour | Use when |
|---|---|---|
| `fixed_window` (default) | Hard window resets at a fixed boundary | Simplicity matters; predictable behaviour preferred |
| `sliding_window` | Blends two adjacent windows to smooth bursts | Burst smoothing matters; not suitable for small limits like `5/minute` |
| `moving_window` | Timestamps every request; strictest accuracy | Precision matters more than memory |
| `token_bucket` | Tokens accumulate over time up to a cap | Controlled bursts with a sustained average rate |

```python
@rate_limit("5/minute", algorithm="fixed_window")   # default
@rate_limit("5/minute", algorithm="sliding_window")
@rate_limit("5/minute", algorithm="moving_window")
@rate_limit("5/minute", algorithm="token_bucket")
```

!!! tip "Choose `fixed_window` for small per-minute limits"
    `sliding_window` re-opens capacity gradually as old requests age out. For small limits like `5/minute` this looks like intermittent blocking to clients — requests alternate between passing and failing within the same window. Use `fixed_window` for predictable, discrete windows.

---

## Burst allowance

Allow extra requests above the base limit to absorb short spikes.

```python
@rate_limit("5/minute", burst=3)   # 8 total requests before blocking
```

Burst is additive. The effective limit becomes `limit + burst`.

---

## Tiered limits

Apply different limits to different user tiers. Pass a `dict` as the first argument — keys are the tier names, values are limit strings.

```python
@rate_limit(
    {"free": "10/minute", "pro": "100/minute", "enterprise": "unlimited"},
    key="user",
)
async def get_reports(request: Request):
    ...
```

The tier is read from `request.state.plan` by default. Set a different attribute name with `tier_resolver`:

```python
@rate_limit(
    {"basic": "20/minute", "premium": "200/minute"},
    key="user",
    tier_resolver="subscription",   # reads request.state.subscription
)
```

Use `"unlimited"` to skip enforcement entirely for that tier.

---

## Exempt IPs and roles

Skip rate limiting for trusted IPs (CIDR notation supported) or user roles.

```python
@rate_limit(
    "10/minute",
    exempt_ips=["127.0.0.1", "10.0.0.0/8", "192.168.0.0/16"],
)
async def internal_metrics():
    ...
```

```python
@rate_limit(
    "10/minute",
    exempt_roles=["admin", "internal-service"],
)
```

Exempt requests bypass the counter entirely. No quota is consumed and no `X-RateLimit-*` headers are injected.

`request.state.user_roles` must be a list or set for role exemption to work. Set it in your auth middleware before the rate limiter runs.

---

## Interaction with maintenance mode

The lifecycle check runs before the rate limit check. A request to a route that is in maintenance mode returns `503` immediately — no counter is incremented. When the route comes back online, the full quota is intact.

```python
@router.get("/checkout")
@maintenance(reason="Payment upgrade")
@rate_limit("20/minute")
async def checkout():
    ...
```

---

## Custom responses

By default, blocked requests return a `429` JSON body. You can replace it with any Starlette `Response` — HTML, plain text, a redirect, or a different JSON shape.

**Per-route** — pass `response=` on the decorator:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
from shield.fastapi.decorators import rate_limit
from shield.core.exceptions import RateLimitExceededException

def rate_limit_response(request: Request, exc: RateLimitExceededException) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "message": "Slow down.",
            "retry_after": exc.retry_after_seconds,
            "support": "https://status.example.com",
        },
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )

@router.get("/public/posts")
@rate_limit("10/minute", response=rate_limit_response)
async def list_posts():
    return {"posts": [...]}
```

**Global default** — set once on `ShieldMiddleware` via the `"rate_limited"` key:

```python
app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "rate_limited": rate_limit_response,
        "maintenance": maintenance_page,  # other keys still work
    },
)
```

**Resolution order:** per-route `response=` → global `responses["rate_limited"]` → built-in 429 JSON.

The factory receives the live `Request` and the `RateLimitExceededException`. Useful attributes on the exception:

| Attribute | Type | Description |
|---|---|---|
| `exc.limit` | `str` | The limit string, e.g. `"10/minute"` |
| `exc.retry_after_seconds` | `int` | Seconds until the window resets |
| `exc.reset_at` | `datetime` | Exact reset timestamp |
| `exc.remaining` | `int` | Always `0` when the exception is raised |
| `exc.key` | `str` | The namespaced counter key that was exceeded |

Both sync and async factories are supported.

---

## Dependency injection

`@rate_limit` works as a FastAPI `Depends()` dependency as well as a decorator. Use `Depends()` when you want to apply rate limiting to a route that uses a plain `APIRouter` (not `ShieldRouter`), or when you need multiple rate limits on a single route.

```python
from fastapi import Depends
from shield.fastapi.decorators import rate_limit

@router.get(
    "/export",
    dependencies=[Depends(rate_limit("5/hour", key="user"))],
)
async def export():
    return {"data": [...]}
```

When used as a dependency, the engine is resolved automatically from `request.app.state.shield_engine` (set by `ShieldMiddleware` at startup). No `engine=` argument is needed.

!!! note "Middleware vs Depends"
    When `@rate_limit` is used as a **decorator** on a `ShieldRouter` route, enforcement happens in `ShieldMiddleware` (before the route handler runs). When used via `Depends()`, enforcement happens in the FastAPI dependency resolution phase. Both increment the same counter and produce the same 429 response.

---

## `@rate_limit` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | `str \| dict` | required | Limit string (`"100/minute"`) or tier dict (`{"free": "10/min", "pro": "100/min"}`) |
| `algorithm` | `str` | `"fixed_window"` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `key` | `str \| callable` | `"ip"` | Key strategy: `"ip"`, `"user"`, `"api_key"`, `"global"`, or an async callable |
| `on_missing_key` | `str \| None` | strategy default | What to do when the key extractor returns `None`: `"exempt"`, `"fallback_ip"`, or `"block"` |
| `burst` | `int` | `0` | Extra requests allowed above the base limit |
| `tier_resolver` | `str` | `"plan"` | `request.state` attribute name used to look up the caller's tier (only applies when `limit` is a dict) |
| `exempt_ips` | `list[str]` | `[]` | IP addresses or CIDR ranges that bypass the limit |
| `exempt_roles` | `list[str]` | `[]` | Roles (from `request.state.user_roles`) that bypass the limit |
| `response` | `callable \| None` | `None` | Custom response factory for this route. See [Custom responses](#custom-responses). |

---

## Runtime policy mutation

Rate limit policies can be changed at runtime without redeploying. Use the CLI or admin dashboard — changes take effect on the next request.

```bash
shield rl list                                          # all registered policies
shield rl set GET:/public/posts 20/minute               # raise the limit
shield rl set GET:/public/posts 5/second --algorithm fixed_window
shield rl reset GET:/public/posts                       # clear counters now
shield rl hits                                          # blocked requests log
shield rl delete GET:/public/posts                      # remove persisted override
```

!!! note "Decorator metadata is the initial state"
    The limit declared in `@rate_limit(...)` is the startup default. If a policy is mutated at runtime and persisted (file or Redis backend), the persisted value takes effect on restart, just like route state.

!!! note "Route must exist"
    `shield rl set` (and `engine.set_rate_limit_policy()`) validate that the route is registered before saving the policy. If the route does not exist, the CLI prints an error and no policy is created. This prevents phantom policies for typos or stale routes.

!!! tip "SDK clients receive changes in real time"
    When using Shield Server + ShieldSDK, policies set via `shield rl set` or the dashboard are broadcast over the SSE stream as typed `rl_policy` events and applied to every connected SDK client immediately — no restart required. The propagation delay is the SSE round-trip (typically under 5 ms on a LAN).

---

## Setting limits from the dashboard (Unprotected Routes)

The **Rate Limits** tab in the admin dashboard (`/shield/rate-limits`) includes an **Unprotected Routes** section that lists every registered route that currently has no rate limit policy. This is useful for:

- Auditing which routes are exposed without throttling
- Adding limits to routes that were not annotated with `@rate_limit` at deploy time

Each row has an **Add Limit** button that opens a modal where you can configure:

| Field | Description |
|---|---|
| Limit | Limit string, e.g. `100/minute` |
| Algorithm | `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| Key strategy | `ip`, `user`, `api_key`, `global` |
| Burst | Extra requests above the base limit |

The HTTP method is read directly from the route key and shown in the modal header — no separate field to fill in.

Submitting the form calls `POST /api/rate-limits` under the hood and redirects back to the Rate Limits page with the new policy visible immediately. The policy behaves identically to one declared with `@rate_limit` — it is persisted, survives restarts (on file or Redis backends), and propagates to SDK clients via SSE.

---

## Per-service rate limit (multi-service)

When running multiple services with Shield Server and `ShieldSDK`, you can set a rate limit that applies to **every route of one service** without decorating individual handlers.

```bash
# Cap all payments-service routes at 1000 per minute per IP
shield srl set payments-service 1000/minute

# Shared counter across all callers
shield srl set payments-service 5000/hour --key global

# Pause and resume enforcement
shield srl disable payments-service
shield srl enable  payments-service

# Clear counters (policy stays in place)
shield srl reset payments-service
```

The service rate limit sits between the all-services global rate limit (`shield grl`) and individual per-route limits. A request passes through all three layers in order before reaching the handler. You can configure any combination of the three independently.

From the dashboard: open the **Rate Limits** tab and filter to a service. A **Service Rate Limit** card appears above the policies table with controls to configure, pause, reset, and remove the policy.

See [Per-service rate limit reference](../reference/rate-limiting.md#per-service-rate-limit) and the [`shield srl` CLI reference](../reference/cli.md#shield-srl--shield-service-rate-limit) for the full API.

---

## Blocked requests log

Every blocked request is recorded. View from the dashboard at `/shield/blocked` or via the CLI:

```bash
shield rl hits
shield rl hits --limit 50
```

The log is capped at 10,000 entries by default (configurable via `max_rl_hit_entries` on the engine). Oldest entries are evicted when the cap is reached.

---

## Backend selection

Rate limit counters are stored separately from route state. The storage is auto-selected based on the main backend:

| Backend | Rate limit storage | Safe for multi-worker? |
|---|---|---|
| `MemoryBackend` | In-process (no lock contention via asyncio.Lock) | No — each worker has its own counter |
| `FileBackend` | In-memory counters + periodic snapshot to disk | No — single process only |
| `RedisBackend` | Redis atomic counters, shared across all workers | Yes |

For production deployments with multiple workers, use `RedisBackend`:

```python
engine = ShieldEngine(backend=RedisBackend("redis://localhost:6379/0"))
```

`create_rate_limit_storage()` automatically uses `RedisRateLimitStorage` when it detects a `RedisBackend`.

---

## Next step

[**Reference: Rate Limiting →**](../reference/rate-limiting.md)
