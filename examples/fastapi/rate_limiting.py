"""FastAPI — Rate Limiting Example.

Demonstrates the full @rate_limit decorator API:

  * Basic IP-based limiting
  * Per-user and per-API-key strategies
  * Global (shared) counters
  * Algorithms: fixed_window, sliding_window, moving_window, token_bucket
  * Burst allowance
  * Tiered limits (free / pro / enterprise)
  * Exempt IPs
  * on_missing_key behaviour (EXEMPT, FALLBACK_IP, BLOCK)
  * Maintenance mode short-circuits the rate limit check
  * Runtime policy mutation via the CLI (persisted to backend)

Run:
    uv run uvicorn examples.fastapi.rate_limiting:app --reload

Then exercise the endpoints:
    curl http://localhost:8000/docs              # Swagger UI
    curl http://localhost:8000/public/posts      # IP-limited: 10/minute
    curl http://localhost:8000/users/me          # per-user: 100/minute
    curl -H "X-API-Key: mykey" \\
         http://localhost:8000/data              # per-API-key: 50/minute
    curl http://localhost:8000/search            # global counter: 5/minute
    curl http://localhost:8000/burst             # 5/minute + burst 3 = 8 total

Admin dashboard (login: admin / secret):
    http://localhost:8000/waygate/

CLI — view and mutate policies at runtime (no redeploy needed):
    waygate login admin
    waygate rl list
    waygate rl set GET:/public/posts 20/minute   # raise the limit live
    waygate rl set POST:/data 10/second --algorithm fixed_window
    waygate rl hits                              # blocked requests log
    waygate rl reset GET:/public/posts           # clear counters
    waygate rl delete GET:/public/posts          # remove persisted override
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from waygate import make_engine
from waygate.fastapi import (
    WaygateAdmin,
    WaygateMiddleware,
    WaygateRouter,
    apply_waygate_to_openapi,
    maintenance,
    rate_limit,
    setup_waygate_docs,
)

engine = make_engine()
router = WaygateRouter(engine=engine)


# ---------------------------------------------------------------------------
# 1. Basic IP-based limiting  (default key strategy)
# ---------------------------------------------------------------------------


@router.get("/public/posts")
@rate_limit("10/minute")
async def list_posts():
    """10 requests/minute per IP address.

    Responses include:
      X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
    Blocked requests return 429 with Retry-After header.
    """
    return {"posts": ["hello", "world"]}


# ---------------------------------------------------------------------------
# 2. Algorithm variants
# ---------------------------------------------------------------------------


@router.get("/fixed")
@rate_limit("5/minute", algorithm="fixed_window")
async def fixed_window_route():
    """Fixed window: counter resets hard at the window boundary.

    Allows bursts at the boundary (all 5 at 00:59 + all 5 at 01:00).
    Use when simplicity matters more than smoothness.
    """
    return {"algorithm": "fixed_window"}


@router.get("/sliding")
@rate_limit("5/minute", algorithm="sliding_window")
async def sliding_window_route():
    """Sliding window: smooths out boundary bursts.

    No request can exceed 5 within any rolling 60-second period.
    This is the default algorithm.
    """
    return {"algorithm": "sliding_window"}


@router.get("/moving")
@rate_limit("5/minute", algorithm="moving_window")
async def moving_window_route():
    """Moving window: strictest — tracks every individual request timestamp."""
    return {"algorithm": "moving_window"}


@router.get("/token-bucket")
@rate_limit("5/minute", algorithm="token_bucket")
async def token_bucket_route():
    """Token bucket: allows controlled bursts, smoothed average rate."""
    return {"algorithm": "token_bucket"}


# ---------------------------------------------------------------------------
# 3. Burst allowance
# ---------------------------------------------------------------------------


@router.get("/burst")
@rate_limit("5/minute", burst=3)
async def burst_route():
    """5/minute base rate + 3 burst = 8 total requests before blocking.

    Burst lets clients absorb a short spike without hitting 429 immediately.
    """
    return {"base": "5/minute", "burst": 3}


# ---------------------------------------------------------------------------
# 4. Per-user limiting  (requires auth middleware to set request.state.user_id)
# ---------------------------------------------------------------------------


@router.get("/users/me")
@rate_limit("100/minute", key="user")
async def get_current_user(request: Request):
    """100 requests/minute per authenticated user.

    Unauthenticated requests (no request.state.user_id) are EXEMPT by default —
    they pass through without consuming the rate limit counter.
    Set on_missing_key="block" to reject unauthenticated callers instead.

    Simulate a logged-in user by setting request.state.user_id in a middleware.
    """
    user_id = getattr(request.state, "user_id", "anonymous")
    return {"user_id": user_id}


@router.get("/users/strict")
@rate_limit("100/minute", key="user", on_missing_key="block")
async def get_user_strict(request: Request):
    """Same limit, but unauthenticated callers get 429 instead of being exempt."""
    user_id = getattr(request.state, "user_id", "anonymous")
    return {"user_id": user_id}


@router.get("/users/fallback")
@rate_limit("100/minute", key="user", on_missing_key="fallback_ip")
async def get_user_fallback(request: Request):
    """When user_id is absent, fall back to IP-based counting."""
    user_id = getattr(request.state, "user_id", "anonymous")
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# 5. Per-API-key limiting
# ---------------------------------------------------------------------------


@router.get("/data")
@rate_limit("50/minute", key="api_key")
async def get_data(request: Request):
    """50 requests/minute per X-API-Key header value.

    When the header is absent, falls back to IP-based limiting (api_key default).
    Send the key via:  curl -H "X-API-Key: mykey" http://localhost:8000/data
    """
    api_key = request.headers.get("X-API-Key", "none")
    return {"api_key": api_key, "data": [1, 2, 3]}


# ---------------------------------------------------------------------------
# 6. Global (shared) counter — all callers share one bucket
# ---------------------------------------------------------------------------


@router.get("/search")
@rate_limit("5/minute", key="global")
async def search():
    """5 requests/minute total across ALL callers.

    Useful for protecting expensive endpoints against aggregate load
    regardless of who is making the requests.
    """
    return {"results": []}


# ---------------------------------------------------------------------------
# 7. Tiered limits — different rates per user plan
#    (requires middleware to set request.state.plan = "free" | "pro" | "enterprise")
# ---------------------------------------------------------------------------


@router.get("/reports")
@rate_limit(
    # Pass a dict to activate tiered mode.
    # Keys are the values of request.state.plan (default tier_resolver).
    # Requests with an unrecognised or missing plan get the "free" tier.
    {"free": "10/minute", "pro": "100/minute", "enterprise": "unlimited"},
    key="user",
)
async def get_reports(request: Request):
    """Tiered rate limiting based on request.state.plan.

    free        → 10/minute
    pro         → 100/minute
    enterprise  → unlimited (never blocked)

    Set the tier in a middleware or dependency:
        request.state.plan = user.subscription_plan
    """
    plan = getattr(request.state, "plan", "free")
    return {"plan": plan, "reports": []}


# ---------------------------------------------------------------------------
# 8. Exempt IPs — internal services / health checks bypass the limit
# ---------------------------------------------------------------------------


@router.get("/internal/metrics")
@rate_limit(
    "10/minute",
    exempt_ips=["127.0.0.1", "10.0.0.0/8", "192.168.0.0/16"],
)
async def internal_metrics():
    """Rate-limited externally, but localhost and RFC1918 ranges are exempt.

    Useful for monitoring agents and internal services that poll frequently.
    """
    return {"metrics": {"requests": 42}}


# ---------------------------------------------------------------------------
# 9. Maintenance + rate limit interaction
#    Maintenance check runs BEFORE rate limit — quota is never consumed
# ---------------------------------------------------------------------------


@router.get("/checkout")
@maintenance(reason="Payment processor upgrade — back in 30 minutes")
@rate_limit("20/minute")
async def checkout():
    """While in maintenance, every request gets 503, not 429.

    The rate limit counter is never incremented during maintenance so the
    quota is fully preserved when the route comes back online.

    Try it:
        waygate maintenance /checkout --reason "upgrade"   # put in maintenance
        # hammer the endpoint — counters stay at 0
        waygate enable /checkout                           # restore
        # full quota available immediately
    """
    return {"checkout": "ok"}


@router.get("/health")
def health():
    """Always 200 — use this to verify the server is up."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="waygate — Rate Limiting Example",
    description=(
        "Demonstrates `@rate_limit` with all key strategies, algorithms, "
        "burst, tiers, exempt IPs, and runtime policy mutation via the CLI."
    ),
)

app.add_middleware(WaygateMiddleware, engine=engine)
app.include_router(router)
apply_waygate_to_openapi(app, engine)
setup_waygate_docs(app, engine)

app.mount(
    "/waygate",
    WaygateAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/waygate",
    ),
)
