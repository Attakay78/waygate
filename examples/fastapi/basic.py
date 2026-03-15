"""FastAPI — Basic Usage Example.

Demonstrates the core api-shield decorators together with the ShieldAdmin
unified admin interface (dashboard UI + REST API for the CLI).

Run:
    uv run uvicorn examples.fastapi.basic:app --reload

Then visit:
    http://localhost:8000/docs           — filtered Swagger UI
    http://localhost:8000/shield/        — admin dashboard (login: admin / secret)
    http://localhost:8000/shield/audit   — audit log

CLI quick-start (auto-discovers the server URL):
    shield login admin          # password: secret
    shield status
    shield disable /payments --reason "hotfix"
    shield enable /payments

Expected behaviour (dev env — set APP_ENV=production to see /debug return 404):
    GET /health          → 200 always          (@force_active)
    GET /payments        → 503 MAINTENANCE_MODE (@maintenance)
    GET /debug           → 200                 (@env_only("dev"), allowed in dev)
    GET /old-endpoint    → 503 ROUTE_DISABLED   (@disabled)
    GET /v1/users        → 200 + deprecation headers (@deprecated)

Switch to dev to unlock /debug:
    APP_ENV=dev uv run uvicorn examples.fastapi.basic:app --reload
"""

import os

from fastapi import FastAPI

from shield.admin import ShieldAdmin
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    deprecated,
    disabled,
    env_only,
    force_active,
    maintenance,
)

CURRENT_ENV = os.getenv("APP_ENV", "dev")
engine = make_engine(current_env=CURRENT_ENV)

router = ShieldRouter(engine=engine)

# ---------------------------------------------------------------------------
# Routes with shield decorators
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health():
    """Always 200 — bypasses every shield check."""
    return {"status": "ok", "env": CURRENT_ENV}


@router.get("/payments")
@maintenance(reason="Scheduled database migration — back at 04:00 UTC")
async def get_payments():
    """Returns 503 MAINTENANCE_MODE."""
    return {"payments": []}


@router.get("/debug")
@env_only("dev")
async def debug():
    """Returns silent 404 in production. Set APP_ENV=dev to unlock."""
    return {"debug": True, "env": CURRENT_ENV}


@router.get("/old-endpoint")
@disabled(reason="Use /v2/endpoint instead")
async def old_endpoint():
    """Returns 503 ROUTE_DISABLED."""
    return {}


@router.get("/v1/users")
@deprecated(sunset="Sat, 01 Jan 2027 00:00:00 GMT", use_instead="/v2/users")
async def v1_users():
    """Returns 200 with Deprecation, Sunset, and Link response headers."""
    return {"users": [{"id": 1, "name": "Alice"}]}


@router.get("/v2/users")
async def v2_users():
    """Active successor to /v1/users."""
    return {"users": [{"id": 1, "name": "Alice"}]}


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="api-shield — Basic Example",
    description=(
        "Core decorators: `@maintenance`, `@disabled`, `@env_only`, "
        "`@force_active`, `@deprecated`.\n\n"
        f"Current environment: **{CURRENT_ENV}**"
    ),
)

app.add_middleware(ShieldMiddleware, engine=engine)
app.include_router(router)
apply_shield_to_openapi(app, engine)

# Mount the unified admin interface:
#   - Dashboard UI  → http://localhost:8000/shield/
#   - REST API      → http://localhost:8000/shield/api/...  (used by the CLI)
#
# auth= accepts:
#   ("user", "pass")              — single user
#   [("alice","a1"),("bob","b2")] — multiple users
#   MyAuthBackend()               — custom ShieldAuthBackend subclass
#
# secret_key= should be a stable value in production so tokens survive
# process restarts. Omit it (or set to None) in development — a random key
# is generated on each startup, invalidating all sessions on restart.
app.mount(
    "/shield",
    ShieldAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/shield",
        # secret_key="change-me-in-production",
        # token_expiry=86400,  # seconds — default 24 h
    ),
)
