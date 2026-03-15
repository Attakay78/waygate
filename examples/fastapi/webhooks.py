"""FastAPI — Webhooks Example.

Demonstrates how api-shield fires HTTP webhooks on every route state change.

This example is fully self-contained: the webhook receivers are mounted on the
same FastAPI app, so no external service is needed. Change a route state via
the CLI or admin dashboard and watch the events appear at /webhook-log.

Run:
    uv run uvicorn examples.fastapi.webhooks:app --reload

Then open two terminals:

  Terminal 1 — watch incoming webhook events:
    watch -n1 curl -s http://localhost:8000/webhook-log

  Terminal 2 — trigger state changes:
    shield config set-url http://localhost:8000/shield
    shield login admin           # password: secret
    shield disable GET:/payments --reason "hotfix"
    shield enable  GET:/payments
    shield maintenance GET:/orders --reason "stock sync"
    shield enable  GET:/orders

Three webhooks are registered on startup:
  1. /webhooks/generic  — raw default_formatter JSON payload
  2. /webhooks/slack    — SlackWebhookFormatter payload (Slack-shaped blocks)
  3. /webhooks/custom   — bespoke formatter defined in this file

Visit http://localhost:8000/docs to explore all endpoints.
"""

import os
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from shield.admin import ShieldAdmin
from shield.core.config import make_engine
from shield.core.models import RouteState
from shield.core.webhooks import SlackWebhookFormatter, default_formatter
from shield.fastapi import (
    ShieldMiddleware,
    ShieldRouter,
    apply_shield_to_openapi,
    disabled,
    force_active,
    maintenance,
)

# ---------------------------------------------------------------------------
# Engine & router
# ---------------------------------------------------------------------------

CURRENT_ENV = os.getenv("APP_ENV", "dev")
engine = make_engine(current_env=CURRENT_ENV)
router = ShieldRouter(engine=engine)

# ---------------------------------------------------------------------------
# In-memory log — stores the last 50 webhook events received
# ---------------------------------------------------------------------------

_webhook_log: deque[dict[str, Any]] = deque(maxlen=50)


# ---------------------------------------------------------------------------
# Custom webhook formatter — bespoke payload shape
# ---------------------------------------------------------------------------


def custom_formatter(event: str, path: str, state: RouteState) -> dict[str, Any]:
    """Minimal custom formatter — returns only the fields our consumer needs."""
    return {
        "source": "api-shield",
        "event": event,
        "route": path,
        "status": state.status,
        "reason": state.reason or None,
        "at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Webhook receiver endpoints  (registered BEFORE webhooks are added so the
# URLs are live when the engine first fires)
# ---------------------------------------------------------------------------


@router.post("/webhooks/generic", include_in_schema=False)
@force_active
async def recv_generic(request: Request) -> dict:
    """Receives the default_formatter JSON payload."""
    body = await request.json()
    _webhook_log.appendleft({"receiver": "generic", "payload": body})
    return {"ok": True}


@router.post("/webhooks/slack", include_in_schema=False)
@force_active
async def recv_slack(request: Request) -> dict:
    """Receives the SlackWebhookFormatter payload."""
    body = await request.json()
    _webhook_log.appendleft({"receiver": "slack", "payload": body})
    return {"ok": True}


@router.post("/webhooks/custom", include_in_schema=False)
@force_active
async def recv_custom(request: Request) -> dict:
    """Receives the custom_formatter payload."""
    body = await request.json()
    _webhook_log.appendleft({"receiver": "custom", "payload": body})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhook log viewer
# ---------------------------------------------------------------------------


@router.get("/webhook-log", include_in_schema=True)
@force_active
async def webhook_log() -> HTMLResponse:
    """Browse the last 50 received webhook events as an HTML page.

    Refresh this page after triggering state changes via the CLI or dashboard.
    """
    import json

    td = "padding:.5rem 1rem;border-bottom:1px solid #e5e7eb"
    if not _webhook_log:
        rows = (
            "<tr><td colspan='3' style='text-align:center;color:#6b7280'>"
            "No events yet — change a route state to trigger a webhook."
            "</td></tr>"
        )
    else:
        rows = ""
        for entry in _webhook_log:
            receiver = entry["receiver"]
            payload = entry["payload"]
            attachments = payload.get("attachments", [{}])
            event = payload.get("event", attachments[0].get("text", "—"))
            at = payload.get("timestamp") or payload.get("at") or "—"
            detail = json.dumps(payload, indent=2)
            rows += (
                f"<tr>"
                f'<td style="{td};font-weight:600">{receiver}</td>'
                f'<td style="{td};font-family:monospace;font-size:.85rem">'
                f"{event}</td>"
                f'<td style="{td}"><details>'
                f'<summary style="cursor:pointer;color:#6b7280">{at}</summary>'
                f'<pre style="font-size:.8rem;margin:.5rem 0 0">{detail}'
                f"</pre></details></td></tr>"
            )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>Webhook Log — api-shield</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem auto; max-width: 960px; color: #111; }}
    h1   {{ font-size: 1.5rem; }}
    p    {{ color: #6b7280; font-size: .9rem; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th    {{ text-align: left; padding: .5rem 1rem; background: #f3f4f6;
             border-bottom: 2px solid #e5e7eb; font-size: .85rem; color: #374151; }}
  </style>
</head>
<body>
  <h1>Webhook Log</h1>
  <p>Showing last {len(_webhook_log)} of up to 50 events. Auto-refreshes every 5 seconds.</p>
  <table>
    <thead><tr><th>Receiver</th><th>Event</th><th>Timestamp / Payload</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Sample shielded routes — change their state to trigger webhooks
# ---------------------------------------------------------------------------


@router.get("/health")
@force_active
async def health() -> dict:
    """Always 200 — use this to verify the server is up."""
    return {"status": "ok", "env": CURRENT_ENV}


@router.get("/payments")
@maintenance(reason="Scheduled DB migration — back at 04:00 UTC")
async def get_payments() -> dict:
    """Starts in maintenance. Enable via CLI: shield enable GET:/payments"""
    return {"payments": []}


@router.get("/orders")
async def get_orders() -> dict:
    """Starts active. Disable or maintenance via CLI to trigger a webhook."""
    return {"orders": []}


@router.get("/legacy")
@disabled(reason="Replaced by /v2/legacy")
async def legacy() -> dict:
    """Permanently disabled — enable it via CLI to fire an enable webhook."""
    return {}


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="api-shield — Webhooks Example",
    description=(
        "Demonstrates webhook notifications on route state changes.\n\n"
        "**Webhook receivers** (all `@force_active`):\n"
        "- `POST /webhooks/generic` — default JSON payload\n"
        "- `POST /webhooks/slack` — Slack Incoming Webhook format\n"
        "- `POST /webhooks/custom` — bespoke minimal payload\n\n"
        "**Webhook log**: `GET /webhook-log` — auto-refreshes every 5 seconds.\n\n"
        "Trigger events via the CLI or admin dashboard and watch them appear."
    ),
)

app.add_middleware(ShieldMiddleware, engine=engine)
app.include_router(router)
apply_shield_to_openapi(app, engine)

# ---------------------------------------------------------------------------
# Register webhooks — all three point at this same app (self-contained)
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

engine.add_webhook(f"{BASE_URL}/webhooks/generic", formatter=default_formatter)
engine.add_webhook(f"{BASE_URL}/webhooks/slack", formatter=SlackWebhookFormatter())
engine.add_webhook(f"{BASE_URL}/webhooks/custom", formatter=custom_formatter)

# ---------------------------------------------------------------------------
# Mount the admin dashboard + REST API (required for the CLI)
# ---------------------------------------------------------------------------

app.mount(
    "/shield",
    ShieldAdmin(engine=engine, auth=("admin", "secret"), prefix="/shield"),
)
