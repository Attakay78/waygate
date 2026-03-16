"""FastAPI — Dependency Injection Example.

Shows how to use shield decorators as FastAPI ``Depends()`` dependencies
instead of (or alongside) the middleware model — but not both on the
same route. Pick one: decorator (with ShieldMiddleware) or Depends()
(without middleware).

Call ``configure_shield(app, engine)`` once and all decorator deps find the
engine automatically via ``request.app.state.shield_engine`` — no ``engine=``
argument per route. ``ShieldMiddleware`` calls ``configure_shield``
automatically at ASGI startup, so if you use middleware you don't need to
call it manually.

Use either the decorator (with ShieldMiddleware) or ``Depends()`` (without
middleware) — not both on the same route.

Decorator support as ``Depends()``:

  ✅ maintenance  — raises 503 when route is in maintenance
  ✅ disabled     — raises 503 when route is disabled
  ✅ env_only     — raises 404 when accessed from the wrong environment
  ✅ deprecated   — injects Deprecation / Sunset / Link headers on the response
  ❌ force_active — decorator-only; shield checks run in the middleware, which
                    completes before any dependency is resolved. A dependency
                    has no mechanism to retroactively bypass that check.

Run:
    uv run uvicorn examples.fastapi.dependency_injection:app --reload

Admin dashboard:
    http://localhost:8000/shield/        — login: admin / secret

CLI quick-start:
    shield login admin          # password: secret
    shield status               # see all route states
    shield enable /payments     # toggle off maintenance without redeploy
    shield disable /payments --reason "emergency patch"

Try these requests:

    curl -i http://localhost:8000/payments     # → 503 MAINTENANCE_MODE
    shield enable /payments                    # toggle off without redeploy
    curl -i http://localhost:8000/payments     # → 200

    curl -i http://localhost:8000/old-endpoint # → 503 ROUTE_DISABLED
    curl -i http://localhost:8000/debug        # → 404 in production env; set APP_ENV=production
    curl -i http://localhost:8000/v1/users     # → 200 + Deprecation headers
    curl -i http://localhost:8000/health       # → 200 always
"""

import os

from fastapi import Depends, FastAPI

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
# App assembly — configure_shield is called automatically by ShieldMiddleware
# ---------------------------------------------------------------------------

app = FastAPI(
    title="api-shield — Dependency Injection Example",
    description=(
        "``configure_shield(app, engine)`` called once — no ``engine=`` per route.\n\n"
        f"Current environment: **{CURRENT_ENV}**"
    ),
)

# ShieldMiddleware auto-calls configure_shield(app, engine) at ASGI startup.
# Without middleware: from shield.fastapi import configure_shield
#                     configure_shield(app, engine)
app.add_middleware(ShieldMiddleware, engine=engine)

# ---------------------------------------------------------------------------
# Routes — engine resolved from app.state; no engine= needed per route
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health():
    """Always 200."""
    return {"status": "ok", "env": CURRENT_ENV}


@router.get("/users")
async def list_users():
    return {"users": [{"id": 1, "name": "Alice"}]}


# Depends() — enforces at the handler level without requiring ShieldMiddleware.
@router.get(
    "/payments",
    dependencies=[Depends(maintenance(reason="Scheduled DB migration"))],
)
async def get_payments():
    """503 on startup; toggle off with: shield enable /payments"""
    return {"payments": []}


@router.get(
    "/old-endpoint",
    dependencies=[Depends(disabled(reason="Use /v2/endpoint instead"))],
)
async def old_endpoint():
    """503 on startup; re-enable with: shield enable /old-endpoint"""
    return {}


@router.get(
    "/debug",
    dependencies=[Depends(env_only("dev", "staging"))],
)
async def debug():
    """404 in production; 200 in dev/staging."""
    return {"env": CURRENT_ENV}


# @deprecated as a Depends() — injects Deprecation, Sunset, and Link headers
# directly on the response at the handler level.
@router.get(
    "/v1/users",
    dependencies=[
        Depends(
            deprecated(
                sunset="Sat, 01 Jan 2027 00:00:00 GMT",
                use_instead="/v2/users",
            )
        )
    ],
)
async def v1_users():
    """200 always, but carries Deprecation + Sunset + Link response headers."""
    return {"users": [{"id": 1, "name": "Alice"}]}


@router.get("/v2/users")
async def v2_users():
    """Active successor to /v1/users."""
    return {"users": [{"id": 1, "name": "Alice"}]}


# @force_active cannot be used as a Depends() — see module docstring for why.
# It is applied as a decorator only.


app.include_router(router)
apply_shield_to_openapi(app, engine)

# ---------------------------------------------------------------------------
# Admin interface — dashboard UI + REST API (used by the CLI)
# ---------------------------------------------------------------------------

app.mount(
    "/shield",
    ShieldAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/shield",
        # secret_key="change-me-in-production",
    ),
)
