"""FastAPI — Scheduled Maintenance Window Example.

Demonstrates how to schedule a future maintenance window that auto-activates
and auto-deactivates at the specified times.

Run:
    uv run uvicorn examples.fastapi.scheduled_maintenance:app --reload

Endpoints:
    GET /orders          — active normally; enters maintenance during the window
    GET /admin/schedule  — schedules a maintenance window 5 seconds from now
    GET /admin/status    — shows current route states
    GET /health          — always 200 (@force_active)

Quick demo:
    1. Open http://localhost:8000/orders          → 200
    2. Hit  http://localhost:8000/admin/schedule  → schedules window
    3. Wait ~5 seconds
    4. Hit  http://localhost:8000/orders          → 503 MAINTENANCE_MODE
    5. Wait another 10 seconds (window ends)
    6. Hit  http://localhost:8000/orders          → 200 again
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import MaintenanceWindow
from shield.fastapi import ShieldMiddleware, ShieldRouter, force_active


engine = ShieldEngine(backend=MemoryBackend())
router = ShieldRouter(engine=engine)


@router.get("/orders")
async def get_orders():
    """Returns 200 normally; 503 during the scheduled maintenance window."""
    return {"orders": [{"id": 1, "total": 49.99}, {"id": 2, "total": 129.00}]}


@router.get("/admin/schedule")
@force_active
async def schedule_maintenance():
    """Schedule a 10-second maintenance window starting 5 seconds from now."""
    now = datetime.now(UTC)
    window = MaintenanceWindow(
        start=now + timedelta(seconds=5),
        end=now + timedelta(seconds=15),
        reason="Automated order system upgrade",
    )
    await engine.schedule_maintenance("GET:/orders", window=window, actor="demo")
    return {
        "scheduled": True,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "message": "GET /orders will enter maintenance in 5 seconds for 10 seconds",
    }


@router.get("/admin/status")
@force_active
async def admin_status():
    """Current shield state for all registered routes."""
    states = await engine.list_states()
    return {
        "routes": [
            {"path": s.path, "status": s.status, "reason": s.reason}
            for s in states
        ]
    }


@router.get("/health")
@force_active
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# App assembly — scheduler is started inside ShieldEngine automatically
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.start_scheduler()
    yield
    await engine.stop_scheduler()


app = FastAPI(
    title="api-shield — Scheduled Maintenance Example",
    description=(
        "Hit `/admin/schedule` to trigger a 10-second maintenance window on "
        "`GET /orders`. The window activates and deactivates automatically."
    ),
    lifespan=lifespan,
)

app.add_middleware(ShieldMiddleware, engine=engine)
app.include_router(router)
