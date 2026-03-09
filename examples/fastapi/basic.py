"""FastAPI — Basic Usage Example.

Demonstrates the core api-shield decorators on both ShieldRouter and a plain
FastAPI APIRouter.

Run:
    uv run uvicorn examples.fastapi.basic:app --reload

Then visit:
    http://localhost:8000/docs   — filtered Swagger UI
    http://localhost:8000/redoc  — filtered ReDoc

Expected behaviour (production env):
    GET /health          → 200 always          (@force_active)
    GET /payments        → 503 MAINTENANCE_MODE (@maintenance)
    GET /debug           → 404 silent           (@env_only("dev"))
    GET /old-endpoint    → 503 ROUTE_DISABLED   (@disabled)
    GET /v1/users        → 200 + deprecation headers (@deprecated)

Switch to dev to unlock /debug:
    APP_ENV=dev uv run uvicorn examples.fastapi.basic:app --reload
"""

import os

from fastapi import FastAPI

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

CURRENT_ENV = os.getenv("APP_ENV", "production")
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
