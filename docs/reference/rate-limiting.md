# Rate Limiting Reference

Full API reference for `@rate_limit`, rate limit models, engine methods, and CLI commands.

---

## `@rate_limit` decorator

```python
from shield.fastapi.decorators import rate_limit
```

Declares a rate limit policy on a route. The policy is registered by `ShieldRouter` at startup and enforced by `ShieldMiddleware` on every matching request.

```python
@router.get("/public/posts")
@rate_limit("10/minute")
async def list_posts():
    ...
```

### Signature

```python
def rate_limit(
    limit: str | dict[str, str],
    *,
    algorithm: str = "fixed_window",
    key: str | Callable = "ip",
    on_missing_key: str | None = None,
    burst: int = 0,
    tier_resolver: str = "plan",
    exempt_ips: list[str] | None = None,
    exempt_roles: list[str] | None = None,
) -> Callable
```

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | `str \| dict` | required | Limit string (`"100/minute"`) or tier dict (`{"free": "10/min", "pro": "100/min"}`) |
| `algorithm` | `str` | `"fixed_window"` | Counting algorithm. One of: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `key` | `str \| callable` | `"ip"` | Key strategy. One of: `"ip"`, `"user"`, `"api_key"`, `"global"`, or a sync/async callable `(Request) -> str \| None` |
| `on_missing_key` | `str \| None` | strategy default | Behaviour when the key extractor returns `None`. One of: `"exempt"`, `"fallback_ip"`, `"block"` |
| `burst` | `int` | `0` | Extra requests allowed above `limit` (additive) |
| `tier_resolver` | `str` | `"plan"` | `request.state` attribute name used to look up the caller's tier. Only applies when `limit` is a dict. |
| `exempt_ips` | `list[str] \| None` | `[]` | IP addresses or CIDR ranges that bypass the limit entirely |
| `exempt_roles` | `list[str] \| None` | `[]` | Roles (from `request.state.user_roles`) that bypass the limit entirely |
| `response` | `callable \| None` | `None` | Custom response factory for rate limit violations. See [Custom responses](#custom-responses). |

### Custom responses

Replace the default 429 JSON body with any Starlette `Response`.

**Per-route** — `response=` on the decorator:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
from shield.core.exceptions import RateLimitExceededException

def my_429(request: Request, exc: RateLimitExceededException) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "retry_after": exc.retry_after_seconds},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )

@router.get("/posts")
@rate_limit("10/minute", response=my_429)
async def list_posts(): ...
```

**Global default** — `responses["rate_limited"]` on `ShieldMiddleware`:

```python
app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={"rate_limited": my_429},
)
```

**Resolution order:** per-route `response=` → `responses["rate_limited"]` → built-in 429 JSON.

Factory signature:

```python
def factory(request: Request, exc: RateLimitExceededException) -> Response: ...
# or async:
async def factory(request: Request, exc: RateLimitExceededException) -> Response: ...
```

Useful `exc` attributes: `limit`, `retry_after_seconds`, `reset_at`, `remaining`, `key`.

### Dependency injection

`@rate_limit` works as a `Depends()` dependency. The engine is resolved from `request.app.state.shield_engine` (set automatically by `ShieldMiddleware`).

```python
from fastapi import Depends
from shield.fastapi.decorators import rate_limit

@router.get("/export", dependencies=[Depends(rate_limit("5/hour", key="user"))])
async def export(): ...
```

Both the decorator path and the `Depends()` path use the same counter — they are equivalent in enforcement.

---

## `RateLimitAlgorithm`

```python
from shield.core.rate_limit.models import RateLimitAlgorithm
```

Controls how requests are counted within a window.

| Value | Description |
|---|---|
| `FIXED_WINDOW` | Fixed time buckets. Simple and predictable. The default. Allows boundary bursts (up to 2x in the worst case). |
| `SLIDING_WINDOW` | Blends two adjacent fixed-window counters. Smooths boundary bursts. Not suitable for small limits like `5/minute` where gradual re-allow looks like intermittent blocking. |
| `MOVING_WINDOW` | Timestamps every individual request. Most accurate; highest memory. |
| `TOKEN_BUCKET` | Tokens accumulate over time up to a cap. Good for controlled bursts with a sustained average rate. Currently mapped to `MOVING_WINDOW` — a native implementation will be used when available from the `limits` library. |

---

## `RateLimitKeyStrategy`

```python
from shield.core.rate_limit.models import RateLimitKeyStrategy
```

Controls what value is used as the bucket key for each request.

| Value | Key source | Never missing? | Default `on_missing_key` |
|---|---|---|---|
| `IP` | `X-Forwarded-For`, `X-Real-IP`, or ASGI scope | Yes (falls back to `"unknown"`) | N/A |
| `USER` | `request.state.user_id` | No | `EXEMPT` |
| `API_KEY` | `X-API-Key` header | No | `FALLBACK_IP` |
| `GLOBAL` | Route path (shared by all callers) | Yes | N/A |
| `CUSTOM` | Sync or async callable provided by the caller | No | `EXEMPT` |

---

## `OnMissingKey`

```python
from shield.core.rate_limit.models import OnMissingKey
```

Controls what happens when the configured key strategy cannot extract a key from the request.

| Value | Behaviour |
|---|---|
| `EXEMPT` | Skip the rate limit entirely. No counter is incremented. The response is returned normally with no rate-limit headers. |
| `FALLBACK_IP` | Use the client IP as the key. The request is rate limited, just bucketed by IP rather than the original strategy. |
| `BLOCK` | Return `429` immediately without incrementing any counter. |

The default per strategy is documented in `RateLimitKeyStrategy` above. Override it with `on_missing_key=` on the decorator.

---

## `RateLimitPolicy`

```python
from shield.core.rate_limit.models import RateLimitPolicy
```

Full rate limiting policy for a single route + method combination. Registered by `ShieldRouter` and stored in the backend.

```python
class RateLimitPolicy(BaseModel):
    path: str
    method: str
    limit: str
    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW
    key_strategy: RateLimitKeyStrategy = RateLimitKeyStrategy.IP
    on_missing_key: OnMissingKey | None = None
    burst: int = 0
    tiers: list[RateLimitTier] = []
    tier_resolver: str = "plan"
    exempt_ips: list[str] = []
    exempt_roles: list[str] = []
```

---

## `RateLimitTier`

```python
from shield.core.rate_limit.models import RateLimitTier
```

A named tier for tiered rate limiting.

```python
class RateLimitTier(BaseModel):
    name: str    # matched against request.state.<tier_resolver>
    limit: str   # e.g. "100/minute" or "unlimited"
```

---

## `RateLimitResult`

```python
from shield.core.rate_limit.models import RateLimitResult
```

Result of a single rate limit check. Read by the middleware to build the response.

```python
class RateLimitResult(BaseModel):
    allowed: bool
    limit: str
    remaining: int
    reset_at: datetime
    retry_after_seconds: int      # 0 when allowed
    key: str                       # the actual key used
    tier: str | None               # which tier was applied, if any
    key_was_missing: bool
    missing_key_behaviour: OnMissingKey | None
```

---

## `RateLimitHit`

```python
from shield.core.rate_limit.models import RateLimitHit
```

Record of a single blocked request. Written to the backend on every `429` response.

```python
class RateLimitHit(BaseModel):
    id: str            # UUID4
    timestamp: datetime
    path: str
    method: str
    key: str           # the key that exceeded the limit
    limit: str
    tier: str | None
    reset_at: datetime
```

The log is capped at `max_rl_hit_entries` (default `10_000`) — oldest entries are evicted when the cap is reached.

---

## Engine methods

### `set_rate_limit_policy`

```python
async def set_rate_limit_policy(
    path: str,
    method: str,
    limit: str,
    algorithm: str = "fixed_window",
    key_strategy: str = "ip",
    burst: int = 0,
    actor: str = "system",
    platform: str = "",
) -> RateLimitPolicy
```

Register or update a rate limit policy at runtime. Persists to the backend so other instances and restarts pick it up. The change is logged in the audit log with action `rl_policy_set` (new) or `rl_policy_updated` (existing policy replaced).

```python
await engine.set_rate_limit_policy(
    "/public/posts", "GET", "20/minute", actor="alice"
)
```

---

### `delete_rate_limit_policy`

```python
async def delete_rate_limit_policy(
    path: str,
    method: str,
    actor: str = "system",
) -> None
```

Remove a persisted policy override. If the route has a `@rate_limit(...)` decorator, the decorator's original policy remains active in memory but is no longer stored. Logged with action `rl_policy_deleted`.

```python
await engine.delete_rate_limit_policy("/public/posts", "GET", actor="alice")
```

---

### `reset_rate_limit`

```python
async def reset_rate_limit(
    path: str,
    method: str | None = None,
    actor: str = "system",
) -> None
```

Clear all rate limit counters for a route. When `method` is omitted, counters for all methods on the path are cleared. Logged with action `rl_reset`.

```python
await engine.reset_rate_limit("/public/posts", "GET", actor="alice")
await engine.reset_rate_limit("/public/posts")   # all methods
```

---

### `get_rate_limit_hits`

```python
async def get_rate_limit_hits(
    path: str | None = None,
    limit: int = 100,
) -> list[RateLimitHit]
```

Return blocked request records, newest first. Optionally filter by route path.

```python
hits = await engine.get_rate_limit_hits(limit=50)
hits = await engine.get_rate_limit_hits(path="/public/posts")
```

---

### `list_rate_limit_policies`

```python
async def list_rate_limit_policies() -> list[RateLimitPolicy]
```

Return all registered rate limit policies.

```python
policies = await engine.list_rate_limit_policies()
for p in policies:
    print(p.method, p.path, p.limit)
```

---

## Global rate limit

A global rate limit applies a single policy across **all routes** with higher precedence than per-route limits. It is checked **first** on every request — if the global limit is exceeded the request is rejected immediately and the per-route counter is never touched. Per-route policies only run after the global limit passes (or when the route is exempt, or no global limit is configured).

### `GlobalRateLimitPolicy`

```python
from shield.core.rate_limit.models import GlobalRateLimitPolicy
```

| Field | Type | Default | Description |
|---|---|---|---|
| `limit` | `str` | required | Limit string, e.g. `"1000/minute"` |
| `algorithm` | `str` | `"fixed_window"` | Counting algorithm |
| `key_strategy` | `str` | `"ip"` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `on_missing_key` | `str \| None` | strategy default | Behaviour when the key extractor returns `None` |
| `burst` | `int` | `0` | Extra requests allowed above `limit` |
| `exempt_routes` | `list[str]` | `[]` | Routes skipped by the global limit. Bare path (`"/health"`) exempts all methods; method-prefixed (`"GET:/metrics"`) exempts that method only |
| `enabled` | `bool` | `True` | Whether the policy is currently enforced. `False` = paused (policy kept, counters not incremented) |

### Engine methods

#### `set_global_rate_limit`

```python
async def set_global_rate_limit(
    limit: str,
    *,
    algorithm: str | None = None,
    key_strategy: str | None = None,
    on_missing_key: str | None = None,
    burst: int = 0,
    exempt_routes: list[str] | None = None,
    actor: str = "system",
    platform: str = "",
) -> GlobalRateLimitPolicy
```

Create or replace the global rate limit policy. Persists to the backend. Logged as `global_rl_set` (new) or `global_rl_updated` (replacement).

```python
await engine.set_global_rate_limit(
    "1000/minute",
    key_strategy="ip",
    exempt_routes=["/health", "GET:/metrics"],
    actor="alice",
)
```

---

#### `get_global_rate_limit`

```python
async def get_global_rate_limit() -> GlobalRateLimitPolicy | None
```

Return the current policy, or `None` if not configured.

---

#### `delete_global_rate_limit`

```python
async def delete_global_rate_limit(*, actor: str = "system") -> None
```

Remove the global rate limit policy entirely. Logged as `global_rl_deleted`.

---

#### `reset_global_rate_limit`

```python
async def reset_global_rate_limit(*, actor: str = "system") -> None
```

Clear all global counters so the limit starts fresh. The policy itself is not removed. Logged as `global_rl_reset`.

---

#### `enable_global_rate_limit`

```python
async def enable_global_rate_limit(*, actor: str = "system") -> None
```

Resume a paused global rate limit policy. No-op if already enabled or not configured. Logged as `global_rl_enabled`.

---

#### `disable_global_rate_limit`

```python
async def disable_global_rate_limit(*, actor: str = "system") -> None
```

Pause the global rate limit without removing it. Requests are no longer counted or blocked by the global policy; per-route policies are unaffected. Logged as `global_rl_disabled`.

---

### Dashboard

The **Rate Limits** page includes a Global Rate Limit card above the policies table.

- **Not configured** — compact bar with a "Set Global Limit" button.
- **Active** — info card showing limit, algorithm, key strategy, burst, and exempt routes. Action buttons: Pause, Edit, Reset, Remove.
- **Paused** — same card with a "Paused" badge (grey) and a Resume button instead of Pause. The limit string is dimmed to indicate it is not being enforced.

---

## CLI commands

`shield rl` and `shield rate-limits` are aliases for the same command group — use whichever you prefer.

```bash
shield rl list          # short form
shield rate-limits list # identical
```

### `shield rl list`

Show all registered rate limit policies.

```bash
shield rl list
```

Output:

| Route | Limit | Algorithm | Key Strategy |
|---|---|---|---|
| GET /public/posts | 10/minute | fixed_window | ip |
| GET /search | 5/minute | fixed_window | global |
| GET /users/me | 100/minute | fixed_window | user |

---

### `shield rl set`

Register or update a policy at runtime. Changes take effect on the next request.

```bash
shield rl set <route> <limit>
```

```bash
shield rl set GET:/public/posts 20/minute
shield rl set GET:/public/posts 5/second --algorithm fixed_window
shield rl set GET:/search 10/minute --key global
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |

---

### `shield rl reset`

Clear all rate limit counters for a route immediately. Clients get their full quota back on the next request.

```bash
shield rl reset GET:/public/posts
```

---

### `shield rl delete`

Remove a persisted policy override from the backend.

```bash
shield rl delete GET:/public/posts
```

---

### `shield rl hits`

Show the blocked requests log.

```bash
shield rl hits                    # last 20 entries
shield rl hits --limit 50         # show more
```

| Option | Description |
|---|---|
| `--limit INT` | Maximum entries to display (default: 20) |

---

Now add the global rate limit CLI commands section after `shield rl hits`:

---

### `shield grl` / `shield global-rate-limit`

`shield grl` and `shield global-rate-limit` are aliases for the global rate limit command group.

```bash
shield grl get           # show current policy
shield global-rate-limit get  # identical
```

#### `shield grl get`

Show the current global rate limit policy (limit, algorithm, key strategy, burst, exempt routes, enabled state).

```bash
shield grl get
```

---

#### `shield grl set`

Configure the global rate limit. Creates a new policy or replaces the existing one.

```bash
shield grl set <limit>
```

```bash
shield grl set 1000/minute
shield grl set 500/minute --algorithm sliding_window --key ip
shield grl set 2000/hour --burst 50 --exempt /health --exempt GET:/metrics
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `--burst INT` | Extra requests above the base limit |
| `--exempt TEXT` | Exempt route (repeatable). Bare path or `METHOD:/path` |

---

#### `shield grl delete`

Remove the global rate limit policy entirely.

```bash
shield grl delete
```

---

#### `shield grl reset`

Clear all global rate limit counters. The policy is kept; clients get their full quota back on the next request.

```bash
shield grl reset
```

---

#### `shield grl enable`

Resume a paused global rate limit policy.

```bash
shield grl enable
```

---

#### `shield grl disable`

Pause the global rate limit without removing it. Per-route policies continue to enforce normally.

```bash
shield grl disable
```

---

## Audit log integration

Rate limit policy changes are recorded in the same audit log as route state changes. The `action` field uses the following values:

**Per-route:**

| Action | Badge | When |
|---|---|---|
| `rl_policy_set` | set | New per-route policy registered |
| `rl_policy_updated` | update | Existing per-route policy replaced |
| `rl_reset` | reset | Per-route counters cleared |
| `rl_policy_deleted` | delete | Per-route policy removed |

**Global:**

| Action | Badge | When |
|---|---|---|
| `global_rl_set` | global set | Global policy created |
| `global_rl_updated` | global update | Global policy replaced |
| `global_rl_reset` | global reset | Global counters cleared |
| `global_rl_deleted` | global delete | Global policy removed |
| `global_rl_enabled` | global enabled | Policy resumed after pause |
| `global_rl_disabled` | global disabled | Policy paused |

View in the dashboard at `/shield/audit` or via `shield log`.

---

## Response headers

Every request to a rate-limited route (allowed or blocked) receives:

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
