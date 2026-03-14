"""FastAPI — Dependency Injection Example.

Call ``configure_shield(app, engine)`` once and all decorator deps
(``maintenance``, ``disabled``, ``env_only``) find the engine automatically
via ``request.app.state.shield_engine`` — no ``engine=`` argument per route.

``ShieldMiddleware`` calls ``configure_shield`` automatically at ASGI startup,
so if you use middleware you don't need to call it manually.

Three patterns in one import:

1. **Decorator** — stamps ``__shield_meta__``; ``ShieldRouter`` registers
   state in the backend; ``ShieldMiddleware`` enforces globally.

2. **Dep (zero-config)** — ``Depends(maintenance(reason="..."))`` with
   ``configure_shield`` called once.  Engine resolved from ``app.state``
   automatically; runtime-togglable via CLI/dashboard.

3. **Dep (explicit engine)** — ``Depends(maintenance(reason="...", engine=engine))``.
   Targets a specific engine; useful when running multiple engines.

Run:
    uv run uvicorn examples.fastapi.dependency_injection:app --reload

Setup note: routes must be registered in the engine (via ShieldRouter or
manually) for engine-backed deps to enforce state.  Unregistered routes are
ACTIVE by default (fail-open).

Try these requests:

    curl http://localhost:8000/payments     # → 503 MAINTENANCE_MODE
    shield enable /payments                 # toggle off without redeploy
    curl http://localhost:8000/payments     # → 200

    curl http://localhost:8000/old-endpoint # → 503 ROUTE_DISABLED
    shield enable /old-endpoint             # re-enable
    curl http://localhost:8000/old-endpoint # → 200

    curl http://localhost:8000/debug        # → 404 (production env)
    APP_ENV=dev uv run uvicorn ...          # → 200

    curl http://localhost:8000/health       # → 200 always
"""

import os

from fastapi import Depends, FastAPI

from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    disabled,
    env_only,
    force_active,
    maintenance,
)

CURRENT_ENV = os.getenv("APP_ENV", "production")
engine = make_engine(current_env=CURRENT_ENV)
router = ShieldRouter(engine=engine)

# ---------------------------------------------------------------------------
# App assembly — configure_shield called ONCE
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
# Routes — engine resolved from app.state, no engine= needed anywhere
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health():
    """Always 200."""
    return {"status": "ok", "env": CURRENT_ENV}


@router.get("/users")
async def list_users():
    return {"users": [{"id": 1, "name": "Alice"}]}


@router.get(
    "/payments",
    dependencies=[Depends(maintenance(reason="Scheduled DB migration"))],
)
@maintenance(reason="Scheduled DB migration")  # stamps __shield_meta__ for ShieldRouter
async def get_payments():
    """503 on startup; toggle off with: shield enable /payments"""
    return {"payments": []}


@router.get(
    "/old-endpoint",
    dependencies=[Depends(disabled(reason="Use /v2/endpoint instead"))],
)
@disabled(reason="Use /v2/endpoint instead")  # stamps __shield_meta__ for ShieldRouter
async def old_endpoint():
    """503 on startup; re-enable with: shield enable /old-endpoint"""
    return {}


@router.get(
    "/debug",
    dependencies=[Depends(env_only("dev", "staging"))],
)
@env_only("dev", "staging")  # stamps __shield_meta__ for ShieldRouter
async def debug():
    """404 in production; 200 in dev/staging."""
    return {"env": CURRENT_ENV}


app.include_router(router)
apply_shield_to_openapi(app, engine)
