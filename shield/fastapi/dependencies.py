"""FastAPI dependency injection helpers for api-shield.

The primary DI tool is :class:`ShieldGuard` — an engine-backed dependency
that enforces whatever state the backend currently holds for a route.

For inline stateless guards, the decorator factories (``maintenance``,
``disabled``, ``env_only``) are themselves dual-purpose: the same object
that stamps ``__shield_meta__`` when used as a decorator also works as a
FastAPI dependency when passed to ``Depends()``::

    from shield.fastapi import maintenance, disabled, env_only, ShieldGuard

    # Engine-backed: state changeable at runtime via CLI / dashboard
    guard = ShieldGuard(engine)

    @router.get("/orders", dependencies=[Depends(guard)])
    async def orders(): ...

    # Inline stateless: always-on, no backend required
    @router.get("/payments", dependencies=[Depends(maintenance(reason="Migration"))])
    async def payments(): ...
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from shield.core.engine import ShieldEngine
from shield.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
)
from shield.fastapi.decorators import (
    _build_disabled_exception,
    _build_env_gated_exception,
    _build_maintenance_exception,
    _format_retry_after,
)


def configure_shield(app: Any, engine: ShieldEngine) -> None:
    """Register *engine* on *app.state* so decorator dependencies find it automatically.

    Call this once during application setup when you are **not** using
    ``ShieldMiddleware`` (which configures the engine on ``app.state``
    automatically at ASGI lifespan startup).

    After calling this, ``maintenance()``, ``disabled()``, and ``env_only()``
    used as ``Depends()`` arguments will call ``engine.check()`` at request
    time — no ``engine=`` parameter needed on each decorator call.

    Parameters
    ----------
    app:
        The FastAPI (or Starlette) application instance.
    engine:
        The ``ShieldEngine`` to register.

    Example
    -------
    ::

        engine = ShieldEngine()
        app = FastAPI()
        configure_shield(app, engine)   # once — all deps find the engine

        @router.get("/payments", dependencies=[Depends(maintenance(reason="Migration"))])
        async def payments(): ...
    """
    app.state.shield_engine = engine


class ShieldGuard:
    """FastAPI dependency that runs ``engine.check()`` for the current route.

    This mirrors what ``ShieldMiddleware`` does globally, but scoped to a
    single route.  Use it when:

    - You prefer not to add ``ShieldMiddleware`` globally (e.g. only a few
      routes need lifecycle management).
    - You want the shield check to appear explicitly in the route signature
      so that readers of the code can see the dependency at a glance.
    - You want the enforcement to participate in FastAPI's DI graph
      (e.g. gated behind an auth dependency).

    The dependency enforces whatever state is currently stored in the backend
    for that route — the same state the middleware would enforce.  Runtime
    state changes (via CLI or dashboard) are reflected immediately.

    Parameters
    ----------
    engine:
        The ``ShieldEngine`` that owns all route state.

    Example
    -------
    ::

        guard = ShieldGuard(engine)

        @router.get("/payments", dependencies=[Depends(guard)])
        async def get_payments():
            return {"payments": []}
    """

    def __init__(self, engine: ShieldEngine) -> None:
        self.engine = engine

    async def __call__(self, request: Request) -> None:
        """Run ``engine.check()`` and raise ``HTTPException`` if the route is blocked."""
        path = request.url.path
        try:
            await self.engine.check(path, method=request.method)
        except MaintenanceException as exc:
            retry_after = _format_retry_after(exc.retry_after)
            raise _build_maintenance_exception(path, exc.reason, retry_after)
        except RouteDisabledException as exc:
            raise _build_disabled_exception(path, exc.reason)
        except EnvGatedException as exc:
            raise _build_env_gated_exception(request.url.path, exc.current_env, exc.allowed_envs)
