"""FastAPI — Custom Responses Example.

Demonstrates two ways to override the default JSON error response:

1. Per-route  — pass ``response=`` directly to the decorator
2. Global     — pass ``responses=`` to ``ShieldMiddleware`` as the app-wide default

Resolution order: per-route ``response=`` → global default → built-in JSON.

Run:
    uv run uvicorn examples.fastapi.custom_responses:app --reload

Then visit:
    http://localhost:8000/docs           — Swagger UI
    http://localhost:8000/shield/        — admin dashboard (login: admin / secret)

Try each blocked route to see its custom response:
    GET /payments    → HTML maintenance page (per-route, 503)
    GET /orders      → redirect to /status  (per-route, 302)
    GET /inventory   → global HTML default  (no per-route factory, falls back)
    GET /reports     → global HTML default  (async factory on the global default)
    GET /legacy      → custom plain text    (per-route on @disabled, 503)
    GET /status      → always 200           (the redirect target)
    GET /health      → always 200           (@force_active)
"""

import os

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from shield.admin import ShieldAdmin
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    disabled,
    force_active,
    maintenance,
)

CURRENT_ENV = os.getenv("APP_ENV", "dev")
engine = make_engine(current_env=CURRENT_ENV)
router = ShieldRouter(engine=engine)


# ---------------------------------------------------------------------------
# Shared response factories
# ---------------------------------------------------------------------------


def maintenance_html(request: Request, exc: Exception) -> HTMLResponse:
    """Reusable branded maintenance page — passed as response= or used globally."""
    reason = getattr(exc, "reason", "Temporarily unavailable")
    retry_after = getattr(exc, "retry_after", None)

    retry_line = f"<p>Expected back: <strong>{retry_after}</strong></p>" if retry_after else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Down for Maintenance</title>
  <style>
    body  {{ font-family: sans-serif; display: flex; flex-direction: column;
             align-items: center; justify-content: center; min-height: 100vh;
             margin: 0; background: #f9fafb; color: #111; }}
    h1    {{ font-size: 2rem; margin-bottom: .5rem; }}
    p     {{ color: #6b7280; }}
    .tag  {{ background: #fef3c7; color: #92400e; padding: .25rem .75rem;
             border-radius: 9999px; font-size: .85rem; font-weight: 600; }}
  </style>
</head>
<body>
  <span class="tag">Maintenance</span>
  <h1>We'll be right back</h1>
  <p>{reason}</p>
  {retry_line}
  <p>Check <a href="/status">our status page</a> for live updates.</p>
</body>
</html>"""
    return HTMLResponse(html, status_code=503)


def disabled_html(request: Request, exc: Exception) -> HTMLResponse:
    """Global default for @disabled routes."""
    reason = getattr(exc, "reason", "This page is no longer available")
    return HTMLResponse(
        f"<h1>This page is gone</h1><p>{reason}</p><p><small>{request.url.path}</small></p>",
        status_code=503,
    )


async def async_maintenance_html(request: Request, exc: Exception) -> HTMLResponse:
    """Async variant — useful when the response body requires an awaited call
    (template rendering, database lookup, etc.)."""
    reason = getattr(exc, "reason", "Unavailable")
    # In a real app: html = await templates.render("maintenance.html", reason=reason)
    html = f"<h1>Unavailable</h1><p>{reason}</p><a href='/status'>Status</a>"
    return HTMLResponse(html, status_code=503)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health() -> dict:
    """Always 200 — bypasses every shield check."""
    return {"status": "ok"}


@router.get("/status")
@force_active
async def status_page() -> dict:
    """Public status endpoint — the redirect target for /orders."""
    return {"operational": True, "message": "Some services are under maintenance."}


# --- Per-route custom responses ---


@router.get("/payments")
@maintenance(reason="Database migration — back at 04:00 UTC", response=maintenance_html)
async def get_payments() -> dict:
    """Per-route HTML response — overrides the global default for this route only."""
    return {"payments": []}


@router.get("/orders")
@maintenance(
    reason="Order service upgrade",
    response=lambda *_: RedirectResponse(url="/status", status_code=302),
)
async def get_orders() -> dict:
    """Per-route redirect — sends users to /status instead of an error body."""
    return {"orders": []}


@router.get("/legacy")
@disabled(
    reason="Retired. Use /v2/orders instead.",
    response=lambda req, exc: PlainTextResponse(f"Gone. {exc.reason}", status_code=503),
)
async def legacy() -> dict:
    """Per-route plain text on a @disabled route."""
    return {}


# --- Routes that fall back to the global default ---


@router.get("/inventory")
@maintenance(reason="Stock sync in progress")
async def get_inventory() -> dict:
    """No per-route response= set — falls back to middleware responses["maintenance"]."""
    return {"items": []}


@router.get("/reports")
@maintenance(reason="Report generation paused for system upgrade")
async def get_reports() -> dict:
    """Also falls back to the global default (async factory)."""
    return {"reports": []}


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="api-shield — Custom Responses",
    description=(
        "Shows two ways to customise blocked-route responses:\n\n"
        "**Per-route**: `@maintenance(response=my_factory)` — overrides for one route.\n\n"
        "**Global default**: `ShieldMiddleware(responses={...})` — applies to all routes "
        "that have no per-route factory.\n\n"
        "Resolution order: per-route → global default → built-in JSON."
    ),
)

# Global defaults — apply to any route that does NOT have response= on its decorator.
# Per-route factories always win.
app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "maintenance": async_maintenance_html,  # async factory works here too
        "disabled": lambda *args: HTMLResponse(
            f"<h1>This page is gone</h1><p>{args[1].reason}</p>", status_code=503
        ),
        # "env_gated": ...  # omit to keep the default silent 404
    },
)

app.include_router(router)
apply_shield_to_openapi(app, engine)

app.mount(
    "/shield",
    ShieldAdmin(engine=engine, auth=("admin", "secret"), prefix="/shield"),
)
