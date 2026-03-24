"""ShieldAdmin — unified admin ASGI app.

Replaces :func:`~shield.dashboard.app.ShieldDashboard` as the recommended
entry-point.  A single mount provides:

* **Dashboard UI** — the HTMX / Jinja2 admin panel (same as before)
* **REST API** — JSON endpoints the ``shield`` CLI uses as its back-end
* **Auth** — token-based session management shared by both surfaces

Usage::

    from shield.admin import ShieldAdmin

    admin = ShieldAdmin(engine=engine, auth=("admin", "secret"))
    app.mount("/shield", admin)

The CLI then points at the same URL::

    shield config set-url http://localhost:8000/shield
    shield login admin          # prompts for password
    shield status

Auth config
-----------
``auth`` accepts:

* ``None``                          — open access (no credentials required)
* ``("user", "pass")``              — single user
* ``[("user", "pass"), …]``         — multiple users with separate credentials
* :class:`~shield.admin.auth.ShieldAuthBackend` subclass instance — custom

Dashboard sessions
------------------
After logging in via ``/login`` the browser receives an ``HttpOnly`` session
cookie (``shield_session``).  The actor stored in audit entries is the
authenticated username; platform is ``"dashboard"``.

CLI tokens
----------
``POST /api/auth/login`` returns a bearer token the CLI stores in
``~/.shield/config.json``.  All subsequent CLI requests send
``X-Shield-Token: <token>``; the actor is the authenticated username
and platform is ``"cli"``.

No-auth mode
------------
When ``auth=None`` every request is accepted.  The actor defaults to
``"anonymous"`` unless the client sends an ``X-Shield-Actor`` header, in
which case that value is used (useful for audit trails in trusted internal
deployments without formal auth).
"""

from __future__ import annotations

import importlib.metadata
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from shield.admin import api as _api
from shield.admin.auth import AuthConfig, TokenManager, auth_fingerprint, make_auth_backend
from shield.core.engine import ShieldEngine
from shield.dashboard import routes as _dash

if TYPE_CHECKING:
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "dashboard" / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "dashboard" / "static"

# Paths that are always accessible without a valid token.
_PUBLIC_PATHS = {"/login", "/logout", "/api/auth/login"}
_PUBLIC_PREFIXES = ("/static/",)


class _AuthMiddleware(BaseHTTPMiddleware):
    """Inject ``shield_actor`` and ``shield_platform`` into ``request.state``.

    * If auth is **not** configured every request passes through; actor is
      taken from the ``X-Shield-Actor`` header or falls back to
      ``"anonymous"``.
    * If auth **is** configured:

      1. Try ``X-Shield-Token: <token>`` (CLI / programmatic access).
      2. Try ``shield_session`` cookie (dashboard browser session).
      3. If neither valid → API paths return 401 JSON; HTML paths redirect
         to ``/login``.

    Login / logout / ``/api/auth/login`` are always accessible so the user
    can authenticate in the first place.
    """

    def __init__(
        self,
        app: ASGIApp,
        token_manager: TokenManager,
        auth_backend: object,
    ) -> None:
        super().__init__(app)
        self._tm = token_manager
        self._has_auth = auth_backend is not None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate credentials, inject actor/platform, or reject / redirect."""
        path = request.url.path  # path within the mounted sub-app (no prefix)

        if not self._has_auth:
            # No auth — allow everything; honour optional X-Shield-Actor hint.
            request.state.shield_actor = request.headers.get("X-Shield-Actor", "") or "anonymous"
            request.state.shield_platform = request.headers.get("X-Shield-Platform", "cli")
            return await call_next(request)

        # Always let public paths and static assets through.
        # NOTE: path is the full URL path (includes the mount prefix), so we use
        # endswith/in rather than exact equality for prefix-agnostic matching.
        if any(path == p or path.endswith(p) for p in _PUBLIC_PATHS) or any(
            pfx in path for pfx in _PUBLIC_PREFIXES
        ):
            request.state.shield_actor = "anonymous"
            request.state.shield_platform = "anonymous"
            return await call_next(request)

        # Try X-Shield-Token header first (CLI / programmatic access).
        token = self._tm.extract_token(request.headers.get("X-Shield-Token", ""))
        # Fall back to session cookie (dashboard).
        if not token:
            token = self._tm.extract_cookie(dict(request.cookies))

        if token:
            result = self._tm.verify(token)
            if result:
                request.state.shield_actor = result[0]
                request.state.shield_platform = result[1]
                return await call_next(request)

        # No valid credentials — decide how to reject.
        is_api = "/api/" in path
        is_cli = request.headers.get("X-Shield-Platform", "") == "cli"

        if is_api or is_cli:
            return JSONResponse(
                {"error": "Authentication required. Use POST /api/auth/login to obtain a token."},
                status_code=401,
            )
        # Browser request — redirect to login page.
        return RedirectResponse(url="login", status_code=302)


async def _login_get(request: Request) -> Response:
    """GET /login — render the login form."""
    tpl: Jinja2Templates = request.app.state.templates
    prefix: str = request.app.state.prefix
    error = request.query_params.get("error", "")
    return tpl.TemplateResponse(
        request,
        "login.html",
        {"prefix": prefix, "error": error, "version": request.app.state.version},
    )


async def _login_post(request: Request) -> Response:
    """POST /login — validate credentials and issue session cookie."""
    tm: TokenManager = request.app.state.token_manager
    auth_backend = request.app.state.auth_backend
    prefix: str = request.app.state.prefix

    if auth_backend is None:
        # No auth configured — redirect to dashboard directly.
        return RedirectResponse(url=f"{prefix}/", status_code=302)

    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))

    if not auth_backend.authenticate_user(username, password):
        return RedirectResponse(url="login?error=Invalid+credentials", status_code=302)

    token, expires_at = tm.create(username, platform="dashboard")
    response = RedirectResponse(url=f"{prefix}/", status_code=302)
    response.set_cookie(
        key=tm.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        max_age=tm.expiry_seconds,
    )
    return response


async def _logout(request: Request) -> Response:
    """GET /logout — revoke session cookie and redirect to login."""
    tm: TokenManager = request.app.state.token_manager
    prefix: str = request.app.state.prefix

    token = tm.extract_cookie(dict(request.cookies))
    if token:
        tm.revoke(token)

    response = RedirectResponse(url=f"{prefix}/login", status_code=302)
    response.delete_cookie(key=tm.COOKIE_NAME)
    return response


def _flag_dashboard_modal_routes() -> list[Route]:
    """Flag + segment modal routes that must be registered BEFORE the generic wildcard.

    These must appear before ``Route("/modal/{action}/{path_key}", ...)`` in the
    route list so Starlette's first-match routing picks the specific handler.
    """
    return [
        Route("/modal/flag/create", _dash.modal_flag_create, methods=["GET"]),
        Route("/modal/flag/{key}/eval", _dash.modal_flag_eval, methods=["GET"]),
        Route("/modal/segment/create", _dash.modal_segment_create, methods=["GET"]),
        Route("/modal/segment/{key}/view", _dash.modal_segment_view, methods=["GET"]),
        Route("/modal/segment/{key}", _dash.modal_segment_detail, methods=["GET"]),
    ]


def _flag_dashboard_routes() -> list[Route]:
    """Return the flag + segment dashboard UI routes for mounting in ShieldAdmin."""
    return [
        Route("/flags", _dash.flags_page, methods=["GET"]),
        Route("/flags/rows", _dash.flags_rows_partial, methods=["GET"]),
        Route("/flags/create", _dash.flag_create_form, methods=["POST"]),
        Route("/flags/{key}", _dash.flag_detail_page, methods=["GET"]),
        Route("/flags/{key}/settings/save", _dash.flag_settings_save, methods=["POST"]),
        Route("/flags/{key}/variations/save", _dash.flag_variations_save, methods=["POST"]),
        Route("/flags/{key}/targeting/save", _dash.flag_targeting_save, methods=["POST"]),
        Route("/flags/{key}/prerequisites/save", _dash.flag_prerequisites_save, methods=["POST"]),
        Route("/flags/{key}/targets/save", _dash.flag_targets_save, methods=["POST"]),
        Route("/flags/{key}/enable", _dash.flag_enable, methods=["POST"]),
        Route("/flags/{key}/disable", _dash.flag_disable, methods=["POST"]),
        Route("/flags/{key}", _dash.flag_delete, methods=["DELETE"]),
        Route("/flags/{key}/eval", _dash.flag_eval_form, methods=["POST"]),
        Route("/segments", _dash.segments_page, methods=["GET"]),
        Route("/segments/rows", _dash.segments_rows_partial, methods=["GET"]),
        Route("/segments/create", _dash.segment_create_form, methods=["POST"]),
        Route("/segments/{key}/rules/add", _dash.segment_rule_add, methods=["POST"]),
        Route("/segments/{key}/rules/{rule_id}", _dash.segment_rule_delete, methods=["DELETE"]),
        Route("/segments/{key}/save", _dash.segment_save_form, methods=["POST"]),
        Route("/segments/{key}", _dash.modal_segment_detail, methods=["GET"]),
        Route("/segments/{key}", _dash.segment_delete, methods=["DELETE"]),
    ]


def _flag_routes() -> list[Route]:
    """Return the flag + segment API routes for mounting in ShieldAdmin."""
    return [
        # ── Flags CRUD ───────────────────────────────────────────────
        Route("/api/flags", _api.list_flags, methods=["GET"]),
        Route("/api/flags", _api.create_flag, methods=["POST"]),
        Route("/api/flags/{key}", _api.get_flag, methods=["GET"]),
        Route("/api/flags/{key}", _api.update_flag, methods=["PUT"]),
        Route("/api/flags/{key}", _api.patch_flag, methods=["PATCH"]),
        Route("/api/flags/{key}", _api.delete_flag, methods=["DELETE"]),
        Route("/api/flags/{key}/enable", _api.enable_flag, methods=["POST"]),
        Route("/api/flags/{key}/disable", _api.disable_flag, methods=["POST"]),
        Route("/api/flags/{key}/evaluate", _api.evaluate_flag, methods=["POST"]),
        # ── Segments CRUD ────────────────────────────────────────────
        Route("/api/segments", _api.list_segments, methods=["GET"]),
        Route("/api/segments", _api.create_segment, methods=["POST"]),
        Route("/api/segments/{key}", _api.get_segment, methods=["GET"]),
        Route("/api/segments/{key}", _api.update_segment, methods=["PUT"]),
        Route("/api/segments/{key}", _api.delete_segment, methods=["DELETE"]),
    ]


def ShieldAdmin(
    engine: ShieldEngine,
    auth: AuthConfig = None,
    token_expiry: int = 86400,
    sdk_token_expiry: int = 31536000,
    secret_key: str | None = None,
    prefix: str = "/shield",
    enable_flags: bool | None = None,
) -> ASGIApp:
    """Create the unified Shield admin ASGI app.

    Mount it on any FastAPI / Starlette application::

        admin = ShieldAdmin(engine=engine, auth=("admin", "secret"))
        app.mount("/shield", admin)

    Parameters
    ----------
    engine:
        The :class:`~shield.core.engine.ShieldEngine` to administer.
    auth:
        Credentials config.  ``None`` = open access.  Accepts a
        ``(username, password)`` tuple, a list of such tuples, or a custom
        :class:`~shield.admin.auth.ShieldAuthBackend` instance.
    token_expiry:
        Session / token lifetime in seconds for dashboard and CLI users.
        Default: 86400 (24 h).  After expiry the user must re-authenticate.
    sdk_token_expiry:
        Token lifetime in seconds for SDK service tokens issued with
        ``platform="sdk"``.  Default: 31536000 (1 year).  This lets
        service apps authenticate once and run indefinitely without
        human intervention, while keeping human user sessions short.
    secret_key:
        HMAC signing key for tokens.  Use a stable value in production so
        tokens survive process restarts.  Defaults to a random key (tokens
        invalidated on restart).
    prefix:
        URL prefix at which the admin app is mounted.  Must match the path
        passed to ``app.mount()``.  Used to build correct redirects.
    enable_flags:
        When ``True``, mount the feature flag and segment dashboard UI and
        REST API endpoints (``/flags/*``, ``/api/flags/*``, ``/api/segments/*``).
        Requires ``engine.use_openfeature()`` to have been called and
        ``api-shield[flags]`` to be installed.
        When ``None`` (default), auto-detected: flags are enabled when
        ``engine.use_openfeature()`` has been called.

    Returns
    -------
    ASGIApp
        A Starlette ASGI application ready to be passed to ``app.mount()``.
    """
    import base64

    # Auto-detect flags: enabled when engine.use_openfeature() has been called.
    if enable_flags is None:
        enable_flags = getattr(engine, "_flag_client", None) is not None

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["encode_path"] = lambda p: (
        base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")
    )
    templates.env.globals["path_slug"] = _dash.path_slug

    def _clean_path(state: object) -> str:
        """Return the display path without the service prefix.

        SDK routes are stored with ``path = "{service}:{original_path}"``.
        This filter strips the prefix so the dashboard shows ``/api/payments``
        rather than ``payments-service:/api/payments``.
        """
        svc = getattr(state, "service", None)
        raw = getattr(state, "path", "")
        if svc and raw.startswith(f"{svc}:"):
            return raw[len(svc) + 1 :]
        return raw

    def _clean_entry_path(entry: object) -> str:
        """Same as _clean_path but works on AuditEntry objects."""
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
    templates.env.globals["flags_enabled"] = enable_flags

    try:
        version = importlib.metadata.version("api-shield")
    except importlib.metadata.PackageNotFoundError:
        version = "0.2.0"

    auth_backend = make_auth_backend(auth)
    token_manager = TokenManager(
        secret_key=secret_key,
        expiry_seconds=token_expiry,
        sdk_token_expiry=sdk_token_expiry,
        auth_fingerprint=auth_fingerprint(auth),
    )

    starlette_app = Starlette(
        routes=[
            Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            # ── Auth (dashboard) ─────────────────────────────────────────
            Route("/login", _login_get, methods=["GET"]),
            Route("/login", _login_post, methods=["POST"]),
            Route("/logout", _logout, methods=["GET"]),
            # ── Dashboard UI ─────────────────────────────────────────────
            Route("/", _dash.index),
            Route("/routes", _dash.routes_partial),
            Route("/modal/global/enable", _dash.modal_global_enable),
            Route("/modal/global/disable", _dash.modal_global_disable),
            Route("/modal/service/enable", _dash.modal_service_enable),
            Route("/modal/service/disable", _dash.modal_service_disable),
            Route("/modal/global-rl", _dash.modal_global_rl),
            Route("/modal/global-rl/delete", _dash.modal_global_rl_delete),
            Route("/modal/global-rl/reset", _dash.modal_global_rl_reset),
            Route("/modal/service-rl", _dash.modal_service_rl),
            Route("/modal/service-rl/delete", _dash.modal_service_rl_delete),
            Route("/modal/service-rl/reset", _dash.modal_service_rl_reset),
            Route("/modal/env/{path_key}", _dash.modal_env_gate),
            # Flag/segment modals must come before the generic wildcard below.
            *(_flag_dashboard_modal_routes() if enable_flags else []),
            Route("/modal/{action}/{path_key}", _dash.action_modal),
            Route(
                "/global-maintenance/enable",
                _dash.global_maintenance_enable,
                methods=["POST"],
            ),
            Route(
                "/global-maintenance/disable",
                _dash.global_maintenance_disable,
                methods=["POST"],
            ),
            Route(
                "/service-maintenance/enable",
                _dash.service_maintenance_enable,
                methods=["POST"],
            ),
            Route(
                "/service-maintenance/disable",
                _dash.service_maintenance_disable,
                methods=["POST"],
            ),
            Route("/toggle/{path_key}", _dash.toggle, methods=["POST"]),
            Route("/disable/{path_key}", _dash.disable, methods=["POST"]),
            Route("/enable/{path_key}", _dash.enable, methods=["POST"]),
            Route("/env/{path_key}", _dash.env_gate, methods=["POST"]),
            Route("/schedule", _dash.schedule, methods=["POST"]),
            Route("/schedule/{path_key}", _dash.cancel_schedule, methods=["DELETE"]),
            Route("/audit", _dash.audit_page),
            Route("/audit/rows", _dash.audit_rows),
            Route("/rate-limits", _dash.rate_limits_page),
            Route("/rate-limits/rows", _dash.rate_limits_rows_partial),
            Route("/rate-limits/hits", _dash.rate_limits_hits_partial),
            Route("/blocked", _dash.rl_hits_page),
            Route("/modal/rl/reset/{path_key}", _dash.modal_rl_reset),
            Route("/modal/rl/edit/{path_key}", _dash.modal_rl_edit),
            Route("/modal/rl/add/{path_key}", _dash.modal_rl_add),
            Route("/modal/rl/delete/{path_key}", _dash.modal_rl_delete),
            Route("/rl/reset/{path_key}", _dash.rl_reset, methods=["POST"]),
            Route("/rl/edit/{path_key}", _dash.rl_edit, methods=["POST"]),
            Route("/rl/add", _dash.rl_add, methods=["POST"]),
            Route("/rl/delete/{path_key}", _dash.rl_delete, methods=["POST"]),
            Route("/global-rl/set", _dash.global_rl_set, methods=["POST"]),
            Route("/global-rl/delete", _dash.global_rl_delete, methods=["POST"]),
            Route("/global-rl/reset", _dash.global_rl_reset, methods=["POST"]),
            Route("/global-rl/enable", _dash.global_rl_enable, methods=["POST"]),
            Route("/global-rl/disable", _dash.global_rl_disable, methods=["POST"]),
            Route("/service-rl/set", _dash.service_rl_set, methods=["POST"]),
            Route("/service-rl/delete", _dash.service_rl_delete, methods=["POST"]),
            Route("/service-rl/reset", _dash.service_rl_reset, methods=["POST"]),
            Route("/service-rl/enable", _dash.service_rl_enable, methods=["POST"]),
            Route("/service-rl/disable", _dash.service_rl_disable, methods=["POST"]),
            Route("/events", _dash.events),
            # ── REST API (CLI) ────────────────────────────────────────────
            Route("/api/auth/login", _api.auth_login, methods=["POST"]),
            Route("/api/auth/logout", _api.auth_logout, methods=["POST"]),
            Route("/api/auth/me", _api.auth_me, methods=["GET"]),
            Route("/api/routes", _api.list_routes, methods=["GET"]),
            Route("/api/routes/{path_key}", _api.get_route, methods=["GET"]),
            Route("/api/routes/{path_key}/enable", _api.enable_route, methods=["POST"]),
            Route("/api/routes/{path_key}/disable", _api.disable_route, methods=["POST"]),
            Route(
                "/api/routes/{path_key}/maintenance",
                _api.maintenance_route,
                methods=["POST"],
            ),
            Route("/api/routes/{path_key}/env", _api.env_route, methods=["POST"]),
            Route("/api/routes/{path_key}/schedule", _api.schedule_route, methods=["POST"]),
            Route(
                "/api/routes/{path_key}/schedule",
                _api.cancel_schedule_route,
                methods=["DELETE"],
            ),
            Route("/api/audit", _api.list_audit, methods=["GET"]),
            Route("/api/global", _api.get_global, methods=["GET"]),
            Route("/api/global/enable", _api.global_enable_api, methods=["POST"]),
            Route("/api/global/disable", _api.global_disable_api, methods=["POST"]),
            Route(
                "/api/services/{service}/maintenance",
                _api.service_maintenance_get,
                methods=["GET"],
            ),
            Route(
                "/api/services/{service}/maintenance/enable",
                _api.service_maintenance_enable,
                methods=["POST"],
            ),
            Route(
                "/api/services/{service}/maintenance/disable",
                _api.service_maintenance_disable,
                methods=["POST"],
            ),
            Route("/api/rate-limits", _api.list_rate_limits, methods=["GET"]),
            Route("/api/rate-limits", _api.set_rate_limit_policy_api, methods=["POST"]),
            Route("/api/rate-limits/hits", _api.get_rate_limit_hits, methods=["GET"]),
            Route(
                "/api/rate-limits/{path_key}/reset",
                _api.reset_rate_limit,
                methods=["DELETE"],
            ),
            Route(
                "/api/rate-limits/{path_key}",
                _api.delete_rate_limit_policy_api,
                methods=["DELETE"],
            ),
            Route("/api/global-rate-limit", _api.get_global_rate_limit, methods=["GET"]),
            Route(
                "/api/global-rate-limit",
                _api.set_global_rate_limit_api,
                methods=["POST"],
            ),
            Route(
                "/api/global-rate-limit",
                _api.delete_global_rate_limit_api,
                methods=["DELETE"],
            ),
            Route(
                "/api/global-rate-limit/reset",
                _api.reset_global_rate_limit_api,
                methods=["DELETE"],
            ),
            Route(
                "/api/global-rate-limit/enable",
                _api.enable_global_rate_limit_api,
                methods=["POST"],
            ),
            Route(
                "/api/global-rate-limit/disable",
                _api.disable_global_rate_limit_api,
                methods=["POST"],
            ),
            Route(
                "/api/services/{service}/rate-limit",
                _api.get_service_rate_limit,
                methods=["GET"],
            ),
            Route(
                "/api/services/{service}/rate-limit",
                _api.set_service_rate_limit_api,
                methods=["POST"],
            ),
            Route(
                "/api/services/{service}/rate-limit",
                _api.delete_service_rate_limit_api,
                methods=["DELETE"],
            ),
            Route(
                "/api/services/{service}/rate-limit/reset",
                _api.reset_service_rate_limit_api,
                methods=["DELETE"],
            ),
            Route(
                "/api/services/{service}/rate-limit/enable",
                _api.enable_service_rate_limit_api,
                methods=["POST"],
            ),
            Route(
                "/api/services/{service}/rate-limit/disable",
                _api.disable_service_rate_limit_api,
                methods=["POST"],
            ),
            # ── SDK endpoints (ShieldServerBackend / ShieldSDK) ──────────
            Route("/api/sdk/events", _api.sdk_events, methods=["GET"]),
            Route("/api/sdk/register", _api.sdk_register, methods=["POST"]),
            Route("/api/sdk/audit", _api.sdk_audit, methods=["POST"]),
            # ── Service discovery ────────────────────────────────────────
            Route("/api/services", _api.list_services, methods=["GET"]),
            # ── Feature flags (mounted only when enable_flags=True) ──────
            *(_flag_dashboard_routes() if enable_flags else []),
            *(_flag_routes() if enable_flags else []),
        ],
    )

    # Inject shared state so both dashboard and API handlers can access it.
    starlette_app.state.engine = engine
    starlette_app.state.templates = templates
    starlette_app.state.prefix = prefix.rstrip("/")
    starlette_app.state.version = version
    starlette_app.state.token_manager = token_manager
    starlette_app.state.auth_backend = auth_backend
    starlette_app.state.flags_enabled = enable_flags

    # Wrap with auth middleware.
    return _AuthMiddleware(starlette_app, token_manager=token_manager, auth_backend=auth_backend)
