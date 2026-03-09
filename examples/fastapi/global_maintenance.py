"""FastAPI — Global Maintenance Mode Example.

Demonstrates enabling and disabling global maintenance mode, which blocks
every route at once without per-route decorators.

Run:
    uv run uvicorn examples.fastapi.global_maintenance:app --reload

Endpoints:
    GET /payments        — normal business route
    GET /orders          — normal business route
    GET /health          — always 200 (@force_active, exempt from global maintenance)
    GET /admin/on        — enable global maintenance
    GET /admin/off       — disable global maintenance
    GET /admin/status    — show global maintenance state

Quick demo:
    1. GET /payments                → 200
    2. GET /admin/on                → enables global maintenance
    3. GET /payments                → 503 MAINTENANCE_MODE
    4. GET /health                  → 200 (force_active routes are exempt by default)
    5. GET /admin/off               → disables global maintenance
    6. GET /payments                → 200 again
"""

from fastapi import FastAPI

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.fastapi import ShieldMiddleware, ShieldRouter, force_active

engine = ShieldEngine(backend=MemoryBackend())
router = ShieldRouter(engine=engine)


# ---------------------------------------------------------------------------
# Business routes
# ---------------------------------------------------------------------------


@router.get("/payments")
async def get_payments():
    """Blocked when global maintenance is enabled."""
    return {"payments": [{"id": 1, "amount": 99.99}]}


@router.get("/orders")
async def get_orders():
    """Blocked when global maintenance is enabled."""
    return {"orders": [{"id": 1, "total": 49.99}]}


@router.get("/health")
@force_active
async def health():
    """Always 200 — @force_active routes bypass global maintenance by default."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin routes — force_active so they stay reachable during maintenance
# ---------------------------------------------------------------------------


@router.get("/admin/on")
@force_active
async def enable_global_maintenance():
    """Enable global maintenance for all routes (except force_active)."""
    await engine.enable_global_maintenance(
        reason="Emergency infrastructure patch",
        exempt_paths=["/health"],
    )
    return {"global_maintenance": "enabled"}


@router.get("/admin/off")
@force_active
async def disable_global_maintenance():
    """Disable global maintenance — all routes resume their per-route state."""
    await engine.disable_global_maintenance()
    return {"global_maintenance": "disabled"}


@router.get("/admin/status")
@force_active
async def admin_status():
    """Current global maintenance configuration."""
    cfg = await engine.get_global_maintenance()
    return {
        "enabled": cfg.enabled,
        "reason": cfg.reason,
        "exempt_paths": cfg.exempt_paths,
        "include_force_active": cfg.include_force_active,
    }


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="api-shield — Global Maintenance Example",
    description=(
        "Hit `/admin/on` to enable global maintenance mode. "
        "All routes return 503 except `@force_active` ones. "
        "Hit `/admin/off` to restore normal operation."
    ),
)

app.add_middleware(ShieldMiddleware, engine=engine)
app.include_router(router)
