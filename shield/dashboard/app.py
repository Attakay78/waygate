"""Shield dashboard — mountable Starlette admin UI factory."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from shield.core.engine import ShieldEngine
from shield.dashboard import routes as r

if TYPE_CHECKING:
    from starlette.types import ASGIApp

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def ShieldDashboard(
    engine: ShieldEngine,
    prefix: str = "/shield",
    auth: tuple[str, str] | None = None,
) -> ASGIApp:
    """Create a mountable Starlette admin UI for the Shield engine.

    Mount it on any FastAPI / Starlette application::

        app.mount("/shield", ShieldDashboard(engine=engine))

    The dashboard is completely self-contained and does **not** affect the
    parent application's routing, OpenAPI schema, or middleware stack.

    Parameters
    ----------
    engine:
        The :class:`~shield.core.engine.ShieldEngine` whose state this
        dashboard will display and control.
    prefix:
        The URL prefix at which the dashboard is mounted.  Used to build
        correct links inside templates.  Should match the path passed to
        ``app.mount()``.  Defaults to ``"/shield"``.
    auth:
        Optional ``(username, password)`` tuple.  When provided, all
        dashboard requests must carry a valid ``Authorization: Basic …``
        header.  Without credentials the dashboard is open to anyone who
        can reach it.

    Returns
    -------
    ASGIApp
        A Starlette ASGI application.  Pass it directly to
        ``app.mount()``.
    """
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Register custom Jinja2 filter so templates can base64-encode path keys
    # for safe embedding in URL segments.
    import base64

    templates.env.filters["encode_path"] = lambda p: (
        base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")
    )

    def _clean_path(state: object) -> str:
        svc = getattr(state, "service", None)
        raw = getattr(state, "path", "")
        if svc and raw.startswith(f"{svc}:"):
            return raw[len(svc) + 1 :]
        return raw

    def _clean_entry_path(entry: object) -> str:
        svc = getattr(entry, "service", None)
        raw = getattr(entry, "path", "")
        # Translate internal sentinel keys to human-friendly labels
        if raw == "__global__":
            return "[Global Maintenance]"
        if raw == "__global_rl__":
            return "[Global Rate Limit]"
        if raw.startswith("__shield:svc_global:") and raw.endswith("__"):
            name = raw[len("__shield:svc_global:") : -2]
            return f"[{name} Maintenance]"
        if raw.startswith("__shield:svc_rl:") and raw.endswith("__"):
            name = raw[len("__shield:svc_rl:") : -2]
            return f"[{name} Rate Limit]"
        if svc and raw.startswith(f"{svc}:"):
            return raw[len(svc) + 1 :]
        return raw

    templates.env.filters["clean_path"] = _clean_path
    templates.env.filters["clean_entry_path"] = _clean_entry_path

    # Expose path_slug as a global so templates can call it without import.
    templates.env.globals["path_slug"] = r.path_slug

    try:
        version = importlib.metadata.version("api-shield")
    except importlib.metadata.PackageNotFoundError:
        version = "0.2.0"

    starlette_app = Starlette(
        routes=[
            Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            Route("/", r.index),
            Route("/routes", r.routes_partial),
            Route("/modal/global/enable", r.modal_global_enable),
            Route("/modal/global/disable", r.modal_global_disable),
            Route("/modal/service/enable", r.modal_service_enable),
            Route("/modal/service/disable", r.modal_service_disable),
            Route("/modal/{action}/{path_key}", r.action_modal),
            Route("/global-maintenance/enable", r.global_maintenance_enable, methods=["POST"]),
            Route("/global-maintenance/disable", r.global_maintenance_disable, methods=["POST"]),
            Route("/service-maintenance/enable", r.service_maintenance_enable, methods=["POST"]),
            Route("/service-maintenance/disable", r.service_maintenance_disable, methods=["POST"]),
            Route("/toggle/{path_key}", r.toggle, methods=["POST"]),
            Route("/disable/{path_key}", r.disable, methods=["POST"]),
            Route("/enable/{path_key}", r.enable, methods=["POST"]),
            Route("/schedule", r.schedule, methods=["POST"]),
            Route("/schedule/{path_key}", r.cancel_schedule, methods=["DELETE"]),
            Route("/audit", r.audit_page),
            Route("/audit/rows", r.audit_rows),
            Route("/rate-limits", r.rate_limits_page),
            Route("/rate-limits/rows", r.rate_limits_rows_partial),
            Route("/rate-limits/hits", r.rate_limits_hits_partial),
            Route("/blocked", r.rl_hits_page),
            Route("/modal/rl/reset/{path_key}", r.modal_rl_reset),
            Route("/modal/rl/edit/{path_key}", r.modal_rl_edit),
            Route("/modal/rl/delete/{path_key}", r.modal_rl_delete),
            Route("/rl/reset/{path_key}", r.rl_reset, methods=["POST"]),
            Route("/rl/edit/{path_key}", r.rl_edit, methods=["POST"]),
            Route("/rl/delete/{path_key}", r.rl_delete, methods=["POST"]),
            Route("/events", r.events),
        ],
    )

    # Inject shared state so route handlers can access engine, templates, etc.
    starlette_app.state.engine = engine
    starlette_app.state.templates = templates
    starlette_app.state.prefix = prefix.rstrip("/")
    starlette_app.state.version = version

    if auth is not None:
        from shield.dashboard.auth import BasicAuthMiddleware

        return BasicAuthMiddleware(starlette_app, username=auth[0], password=auth[1])

    return starlette_app
