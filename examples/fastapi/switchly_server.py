"""FastAPI — Waygate Server Mode Example.

Demonstrates the centralized Waygate Server architecture: a single Waygate
Server process owns all route state, and one or more service apps connect
to it via WaygateSDK.  State is enforced locally on every request with zero
network overhead — the SDK keeps an in-process cache synced over a
persistent SSE connection.

This file defines TWO separate ASGI apps.  Run them in separate terminals:

  App 1 — The Waygate Server (port 8001):
    uv run uvicorn examples.fastapi.waygate_server:waygate_app --port 8001 --reload

  App 2 — The Service App (port 8000):
    uv run uvicorn examples.fastapi.waygate_server:service_app --port 8000 --reload

Then visit:
    http://localhost:8001/           — Waygate Server dashboard (admin / secret)
    http://localhost:8001/audit      — audit log (all services)
    http://localhost:8000/docs       — service Swagger UI

CLI — always points at the Waygate Server, not the service:
    waygate config set-url http://localhost:8001
    waygate login admin              # password: secret
    waygate status                   # routes registered by my-service
    waygate disable /api/orders --reason "hotfix"
    waygate enable /api/orders
    waygate maintenance /api/payments --reason "DB migration"
    waygate audit                    # full audit trail

Expected behaviour:
    GET /health          → 200 always              (@force_active — survives disable)
    GET /api/payments    → 503 MAINTENANCE_MODE    (starts in maintenance)
    GET /api/orders      → 200                     (active on startup)
    GET /api/legacy      → 503 ROUTE_DISABLED      (@disabled)
    GET /api/v1/products → 200 + deprecation hdr   (@deprecated)
    GET /api/v2/products → 200                     (active successor)

Production notes:
    Backend choice affects the Waygate Server only — SDK clients always receive
    live SSE updates regardless of backend, because they connect to the Waygate
    Server over HTTP (not to the backend directly):

    * MemoryBackend  — fine for development; state is lost when the Waygate
                       Server restarts.
    * FileBackend    — state survives restarts; safe for single-server
                       deployments (no multi-process file locking).
    * RedisBackend   — required only when you run multiple Waygate Server
                       instances behind a load balancer (high availability).
                       Cross-instance pub/sub keeps all Waygate Server nodes
                       in sync so every SDK client gets consistent state.

    * Use a stable secret_key so tokens survive Waygate Server restarts.
    * Prefer passing username/password to WaygateSDK so the SDK obtains its
      own sdk-platform token on startup (sdk_token_expiry, default 1 year)
      rather than managing a pre-issued token manually.
    * Set token_expiry (dashboard/CLI sessions) and sdk_token_expiry (service
      tokens) independently so human sessions stay short-lived.
"""

from __future__ import annotations

from fastapi import FastAPI

from waygate import MemoryBackend
from waygate.fastapi import (
    WaygateRouter,
    apply_waygate_to_openapi,
    deprecated,
    disabled,
    force_active,
    maintenance,
)
from waygate.sdk import WaygateSDK
from waygate.server import WaygateServer

# ---------------------------------------------------------------------------
# App 1 — Waygate Server
# ---------------------------------------------------------------------------
# Run: uv run uvicorn examples.fastapi.waygate_server:waygate_app --port 8001 --reload
#
# The Waygate Server is a self-contained ASGI app that exposes:
#   /            — HTMX dashboard UI  (login: admin / secret)
#   /audit       — audit log
#   /api/...     — REST API consumed by the CLI
#   /api/sdk/... — SSE + register endpoints consumed by WaygateSDK clients
#
# For production: swap MemoryBackend for RedisBackend so every connected
# service receives live state updates via the SSE channel.
#
#   from waygate import RedisBackend
#   backend = RedisBackend("redis://localhost:6379")
#
# secret_key should be a stable value so issued tokens survive restarts.
# Omit it (or pass None) in development — a random key is generated per run.

waygate_app = WaygateServer(
    backend=MemoryBackend(),
    auth=("admin", "secret"),
    # secret_key="change-me-in-production",
    # token_expiry=3600,       # dashboard / CLI sessions — default 24 h
    # sdk_token_expiry=31536000,  # SDK service tokens — default 1 year
)

# ---------------------------------------------------------------------------
# App 2 — Service App
# ---------------------------------------------------------------------------
# Run: uv run uvicorn examples.fastapi.waygate_server:service_app --port 8000 --reload
#
# WaygateSDK wires WaygateMiddleware + startup/shutdown lifecycle into the app.
# Route enforcement is purely local — the SDK never adds per-request latency.
#
# Authentication options (choose one):
#
#   1. Auto-login — recommended for production. The SDK calls
#      POST /api/auth/login with platform="sdk" on startup and caches the
#      returned token (valid for sdk_token_expiry, default 1 year).
#      Inject credentials from environment variables:
#
#        sdk = WaygateSDK(
#            server_url="http://localhost:8001",
#            app_id="my-service",
#            username=os.environ["WAYGATE_USERNAME"],
#            password=os.environ["WAYGATE_PASSWORD"],
#        )
#
#   2. Pre-issued token — obtain once via `waygate login`, store as a secret:
#        sdk = WaygateSDK(..., token=os.environ["WAYGATE_TOKEN"])
#
#   3. No auth — omit token/username/password when the Waygate Server has
#      no auth configured (auth=None or auth omitted).

sdk = WaygateSDK(
    server_url="http://localhost:8001",
    app_id="my-service",
    username="admin",
    password="secret",
    # username="admin",   # or inject from env: os.environ["WAYGATE_USERNAME"]
    # password="secret",  # or inject from env: os.environ["WAYGATE_PASSWORD"]
    reconnect_delay=5.0,  # seconds between SSE reconnect attempts
)

service_app = FastAPI(
    title="waygate — Waygate Server Example (Service)",
    description=(
        "Connects to the Waygate Server at **http://localhost:8001** via "
        "WaygateSDK.  All route state is managed centrally — use the "
        "[Waygate Dashboard](http://localhost:8001/) or the CLI to "
        "enable, disable, or pause any route without redeploying."
    ),
)

# attach() adds WaygateMiddleware and wires startup/shutdown hooks.
# Call this BEFORE defining routes so the router below can use sdk.engine.
sdk.attach(service_app)

# WaygateRouter auto-registers decorated routes with the Waygate Server on
# startup so they appear in the dashboard immediately.
router = WaygateRouter(engine=sdk.engine)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health():
    """Always 200 — bypasses every waygate check.

    Use this for load-balancer probes.  @force_active ensures the route
    stays reachable even if the Waygate Server is temporarily unreachable
    and the SDK falls back to its empty local cache.
    """
    return {"status": "ok", "service": "my-service"}


@router.get("/api/payments")
@maintenance(reason="Scheduled database migration — back at 04:00 UTC")
async def get_payments():
    """Returns 503 MAINTENANCE_MODE on startup.

    Lift maintenance from the CLI (no redeploy needed):
        waygate enable /api/payments
    """
    return {"payments": [{"id": 1, "amount": 99.99}]}


@router.get("/api/orders")
async def list_orders():
    """Active on startup — disable from the CLI:

    waygate disable /api/orders --reason "hotfix"
    waygate enable  /api/orders
    """
    return {"orders": [{"id": 42, "status": "shipped"}]}


@router.get("/api/legacy")
@disabled(reason="Use /api/v2/products instead")
async def legacy_endpoint():
    """Returns 503 ROUTE_DISABLED.

    The @disabled state is set at deploy time and can be overridden from
    the dashboard or CLI:
        waygate enable /api/legacy
    """
    return {}


@router.get("/api/v1/products")
@deprecated(sunset="Sat, 01 Jan 2028 00:00:00 GMT", use_instead="/api/v2/products")
async def v1_products():
    """Returns 200 with Deprecation, Sunset, and Link response headers.

    Headers injected by WaygateMiddleware on every response:
        Deprecation: true
        Sunset: Sat, 01 Jan 2028 00:00:00 GMT
        Link: </api/v2/products>; rel="successor-version"
    """
    return {"products": [{"id": 1, "name": "Widget"}], "version": 1}


@router.get("/api/v2/products")
async def v2_products():
    """Active successor to /api/v1/products."""
    return {"products": [{"id": 1, "name": "Widget"}], "version": 2}


service_app.include_router(router)
apply_waygate_to_openapi(service_app, sdk.engine)

# ---------------------------------------------------------------------------
# How the CLI talks to this setup
# ---------------------------------------------------------------------------
#
# The CLI always communicates with the Waygate Server, never directly with
# the service app.  From the Waygate Server's perspective, routes from
# "my-service" appear namespaced as "my-service:/api/payments" etc.
#
#   # One-time setup
#   waygate config set-url http://localhost:8001
#   waygate login admin
#
#   # Inspect state
#   waygate status                               # all routes for my-service
#   waygate audit                                # full audit trail
#
#   # Lifecycle management
#   waygate disable /api/orders --reason "hotfix"
#   waygate enable  /api/orders
#   waygate maintenance /api/payments --reason "scheduled downtime"
#   waygate schedule /api/payments               # set maintenance window
#
#   # Dashboard
#   open http://localhost:8001/                 # full UI, no CLI needed
