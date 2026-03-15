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
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from shield.admin import api as _api
from shield.admin.auth import AuthConfig, TokenManager, auth_fingerprint, make_auth_backend
from shield.core.engine import ShieldEngine
from shield.dashboard import routes as _dash

if TYPE_CHECKING:
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "dashboard" / "templates"

# Paths that are always accessible without a valid token.
_PUBLIC_PATHS = {"/login", "/logout", "/api/auth/login"}


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

        # Always let public paths through (login form, auth API endpoint).
        if any(path == p or path.endswith(p) for p in _PUBLIC_PATHS):
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


def ShieldAdmin(
    engine: ShieldEngine,
    auth: AuthConfig = None,
    token_expiry: int = 86400,
    secret_key: str | None = None,
    prefix: str = "/shield",
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
        Session / token lifetime in seconds.  Default: 86400 (24 h).
        After expiry the user must re-authenticate.
    secret_key:
        HMAC signing key for tokens.  Use a stable value in production so
        tokens survive process restarts.  Defaults to a random key (tokens
        invalidated on restart).
    prefix:
        URL prefix at which the admin app is mounted.  Must match the path
        passed to ``app.mount()``.  Used to build correct redirects.

    Returns
    -------
    ASGIApp
        A Starlette ASGI application ready to be passed to ``app.mount()``.
    """
    import base64

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["encode_path"] = lambda p: (
        base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")
    )
    templates.env.globals["path_slug"] = _dash.path_slug

    try:
        version = importlib.metadata.version("api-shield")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"

    auth_backend = make_auth_backend(auth)
    token_manager = TokenManager(
        secret_key=secret_key,
        expiry_seconds=token_expiry,
        auth_fingerprint=auth_fingerprint(auth),
    )

    starlette_app = Starlette(
        routes=[
            # ── Auth (dashboard) ─────────────────────────────────────────
            Route("/login", _login_get, methods=["GET"]),
            Route("/login", _login_post, methods=["POST"]),
            Route("/logout", _logout, methods=["GET"]),
            # ── Dashboard UI ─────────────────────────────────────────────
            Route("/", _dash.index),
            Route("/routes", _dash.routes_partial),
            Route("/modal/global/enable", _dash.modal_global_enable),
            Route("/modal/global/disable", _dash.modal_global_disable),
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
            Route("/toggle/{path_key}", _dash.toggle, methods=["POST"]),
            Route("/disable/{path_key}", _dash.disable, methods=["POST"]),
            Route("/enable/{path_key}", _dash.enable, methods=["POST"]),
            Route("/schedule", _dash.schedule, methods=["POST"]),
            Route("/schedule/{path_key}", _dash.cancel_schedule, methods=["DELETE"]),
            Route("/audit", _dash.audit_page),
            Route("/audit/rows", _dash.audit_rows),
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
        ],
    )

    # Inject shared state so both dashboard and API handlers can access it.
    starlette_app.state.engine = engine
    starlette_app.state.templates = templates
    starlette_app.state.prefix = prefix.rstrip("/")
    starlette_app.state.version = version
    starlette_app.state.token_manager = token_manager
    starlette_app.state.auth_backend = auth_backend

    # Wrap with auth middleware.
    return _AuthMiddleware(starlette_app, token_manager=token_manager, auth_backend=auth_backend)
