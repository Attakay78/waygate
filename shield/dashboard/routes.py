"""Shield dashboard HTTP route handlers."""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.templating import Jinja2Templates

from shield.core.engine import ShieldEngine
from shield.core.exceptions import RouteProtectedException
from shield.core.models import MaintenanceWindow, RouteState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _engine(request: Request) -> ShieldEngine:
    """Return the ShieldEngine from app state."""
    return request.app.state.engine  # type: ignore[no-any-return]


def _templates(request: Request) -> Jinja2Templates:
    """Return the Jinja2Templates instance from app state."""
    return request.app.state.templates  # type: ignore[no-any-return]


def _prefix(request: Request) -> str:
    """Return the dashboard mount prefix from app state."""
    return request.app.state.prefix  # type: ignore[no-any-return]


def _actor(request: Request) -> str:
    """Return the authenticated actor name (set by auth middleware or default)."""
    return getattr(request.state, "shield_actor", "dashboard")


def _platform(request: Request) -> str:
    """Return the platform from request state (always 'dashboard' for UI actions)."""
    return getattr(request.state, "shield_platform", "dashboard")


# ---------------------------------------------------------------------------
# Path encoding utilities
# ---------------------------------------------------------------------------


def path_slug(path: str) -> str:
    """Convert a route path key to a CSS-safe slug for HTML IDs and SSE events.

    Curly braces from parameterised route templates (e.g. ``{user_id}``) are
    stripped so the resulting slug is a valid CSS identifier.

    Examples
    --------
    ``"/payments"``               → ``"payments"``
    ``"/api/v1/payments"``        → ``"api-v1-payments"``
    ``"GET:/payments"``           → ``"GET--payments"``
    ``"GET:/users/{user_id}"``    → ``"GET--users-user_id"``
    """
    slug = path.lstrip("/")
    # Strip template braces before replacing other special characters so that
    # "/users/{user_id}" becomes "users-user_id" (not "users--user_id-").
    slug = slug.replace("{", "").replace("}", "")
    for char in "/:._":
        slug = slug.replace(char, "-")
    return slug or "root"


def _encode_path(path: str) -> str:
    """Base64url-encode *path* for safe embedding in URL segments."""
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def _decode_path(encoded: str) -> str:
    """Decode a base64url-encoded route path key from a URL segment."""
    # Re-add stripped base64 padding.
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    return base64.urlsafe_b64decode(encoded).decode()


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_SIZE = 20


def _paginate(items: list[Any], page: int, page_size: int = _DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """Slice *items* for the requested *page* and return pagination metadata.

    Returns a dict with:
    - ``items``       — the slice for the current page
    - ``page``        — current page number (1-based, clamped to valid range)
    - ``page_size``   — items per page
    - ``total``       — total number of items
    - ``total_pages`` — total number of pages (minimum 1)
    - ``has_prev``    — True when a previous page exists
    - ``has_next``    — True when a next page exists
    """
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


# ---------------------------------------------------------------------------
# Template rendering helper
# ---------------------------------------------------------------------------


def _render_route_row(tpl: Jinja2Templates, state: RouteState, prefix: str) -> str:
    """Render the ``route_row.html`` partial synchronously and return the HTML string."""
    return tpl.env.get_template("partials/route_row.html").render(
        state=state,
        path_slug=path_slug(state.path),
        prefix=prefix,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _get_services(states: list[RouteState]) -> list[str]:
    return sorted({s.service for s in states if s.service})


def _get_unrated_routes(
    states: list[RouteState],
    policies_dict: dict[str, Any],
    service: str = "",
) -> list[RouteState]:
    """Return route states that have no rate limit policy set.

    Strips service prefix when comparing against policy paths so that SDK
    routes (stored as ``service:/path``) are matched correctly.
    """
    # Policy keys are "METHOD:/path"; extract just the path portion.
    rated_paths = {k.split(":", 1)[1] for k in policies_dict.keys()}
    result = []
    for state in states:
        if service and state.service != service:
            continue
        svc = state.service or ""
        raw = state.path
        display_path = raw[len(svc) + 1 :] if svc and raw.startswith(f"{svc}:") else raw
        if display_path not in rated_paths:
            result.append(state)
    return sorted(result, key=lambda s: s.path)


async def index(request: Request) -> Response:
    """Render the main routes page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    service = request.query_params.get("service", "")
    states = await engine.list_states()
    services = _get_services(states)
    if service:
        states = [s for s in states if s.service == service]
    global_config = await engine.get_global_maintenance()
    service_config = await engine.get_service_maintenance(service) if service else None
    # Build a path → policy dict for the rate limit badge column.
    # Policies are keyed "METHOD:/path" so we index by path only (first match wins).
    rl_by_path: dict[str, object] = {}
    for key, policy in engine._rate_limit_policies.items():
        path_key = key.split(":", 1)[1] if ":" in key else key
        if path_key not in rl_by_path:
            rl_by_path[path_key] = policy
    paged = _paginate(states, page)
    return tpl.TemplateResponse(
        request,
        "index.html",
        {
            "states": paged["items"],
            "pagination": paged,
            "global_config": global_config,
            "service_config": service_config,
            "rate_limit_policies": rl_by_path,
            "prefix": prefix,
            "active_tab": "routes",
            "version": request.app.state.version,
            "path_slug": path_slug,
            "shield_actor": _actor(request),
            "services": services,
            "selected_service": service,
        },
    )


async def routes_partial(request: Request) -> Response:
    """Return only the routes table rows (HTMX polling fallback)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    service = request.query_params.get("service", "")
    states = await engine.list_states()
    if service:
        states = [s for s in states if s.service == service]
    return tpl.TemplateResponse(
        request,
        "partials/routes_table.html",
        {
            "states": states,
            "prefix": prefix,
            "path_slug": path_slug,
            "selected_service": service,
        },
    )


async def toggle(request: Request) -> HTMLResponse:
    """Toggle the route between ``active`` and ``maintenance``.

    If the route is currently in maintenance, enable it.  Otherwise put it
    into maintenance mode.
    """
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])

    form_data = await request.form()
    reason = str(form_data.get("reason", "") or request.headers.get("HX-Prompt", ""))
    try:
        state = await engine.get_state(route_path)
        if state.status.value == "maintenance":
            new_state = await engine.enable(
                route_path, reason=reason, actor=_actor(request), platform=_platform(request)
            )
        else:
            new_state = await engine.set_maintenance(
                route_path,
                reason=reason,
                actor=_actor(request),
                platform=_platform(request),
            )
    except RouteProtectedException:
        new_state = await engine.get_state(route_path)

    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def disable(request: Request) -> HTMLResponse:
    """Disable a route, returning 503 for all subsequent requests."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])

    form_data = await request.form()
    reason = str(form_data.get("reason", "") or request.headers.get("HX-Prompt", ""))
    try:
        new_state = await engine.disable(
            route_path, reason=reason, actor=_actor(request), platform=_platform(request)
        )
    except RouteProtectedException:
        new_state = await engine.get_state(route_path)

    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def enable(request: Request) -> HTMLResponse:
    """Enable a route, restoring it to active status."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])

    form_data = await request.form()
    reason = str(form_data.get("reason", "") or request.headers.get("HX-Prompt", ""))
    try:
        new_state = await engine.enable(
            route_path, reason=reason, actor=_actor(request), platform=_platform(request)
        )
    except RouteProtectedException:
        new_state = await engine.get_state(route_path)

    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def schedule(request: Request) -> HTMLResponse:
    """Schedule a future maintenance window from HTML form data.

    Expected form fields: ``path``, ``start`` (datetime-local), ``end``
    (datetime-local), ``reason`` (optional).
    """
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    form = await request.form()
    route_path = str(form["path"])
    reason = str(form.get("reason", ""))
    start_str = str(form.get("start", ""))
    end_str = str(form.get("end", ""))

    # datetime-local values are ISO-like strings without timezone — treat as UTC.
    start_dt = datetime.fromisoformat(start_str).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(end_str).replace(tzinfo=UTC)

    window = MaintenanceWindow(start=start_dt, end=end_dt, reason=reason)
    try:
        await engine.schedule_maintenance(
            route_path, window, actor=_actor(request), platform=_platform(request)
        )
    except RouteProtectedException:
        pass

    new_state = await engine.get_state(route_path)
    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def cancel_schedule(request: Request) -> HTMLResponse:
    """Cancel a pending scheduled maintenance window."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])

    await engine.scheduler.cancel(route_path)
    new_state = await engine.get_state(route_path)
    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def audit_page(request: Request) -> Response:
    """Render the audit log page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    service = request.query_params.get("service", "")
    all_states = await engine.list_states()
    services = _get_services(all_states)
    entries = await engine.get_audit_log(limit=1000)
    if service:
        entries = [e for e in entries if e.service == service]
    paged = _paginate(entries, page)
    return tpl.TemplateResponse(
        request,
        "audit.html",
        {
            "entries": paged["items"],
            "pagination": paged,
            "prefix": prefix,
            "active_tab": "audit",
            "version": request.app.state.version,
            "shield_actor": _actor(request),
            "services": services,
            "selected_service": service,
        },
    )


async def audit_rows(request: Request) -> Response:
    """Return only the audit log rows partial (for HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)

    service = request.query_params.get("service", "")
    entries = await engine.get_audit_log(limit=50)
    if service:
        entries = [e for e in entries if e.service == service]
    return tpl.TemplateResponse(
        request,
        "partials/audit_rows.html",
        {"entries": entries, "selected_service": service},
    )


def _render_global_widget(tpl: Jinja2Templates, config: object, prefix: str) -> str:
    """Render the global maintenance status widget partial."""
    return tpl.env.get_template("partials/global_maintenance.html").render(
        config=config,
        prefix=prefix,
    )


async def modal_global_enable(request: Request) -> HTMLResponse:
    """Return the global maintenance enable modal form."""
    tpl = _templates(request)
    prefix = _prefix(request)
    html = tpl.env.get_template("partials/modal_global_enable.html").render(prefix=prefix)
    return HTMLResponse(html)


async def modal_global_disable(request: Request) -> HTMLResponse:
    """Return the global maintenance disable confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    html = tpl.env.get_template("partials/modal_global_disable.html").render(prefix=prefix)
    return HTMLResponse(html)


async def global_maintenance_enable(request: Request) -> HTMLResponse:
    """Enable global maintenance mode from form data.

    Expected form fields: ``reason``, ``exempt_paths`` (newline-separated),
    ``include_force_active`` (checkbox, value ``"1"``).
    """
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    form = await request.form()
    reason = str(form.get("reason", ""))
    exempt_raw = str(form.get("exempt_paths", ""))
    exempt_paths = [p.strip() for p in exempt_raw.splitlines() if p.strip()]
    include_force_active = form.get("include_force_active") == "1"

    await engine.enable_global_maintenance(
        reason=reason,
        exempt_paths=exempt_paths,
        include_force_active=include_force_active,
        actor=_actor(request),
        platform=_platform(request),
    )
    config = await engine.get_global_maintenance()
    return HTMLResponse(_render_global_widget(tpl, config, prefix))


async def global_maintenance_disable(request: Request) -> HTMLResponse:
    """Disable global maintenance mode, restoring per-route states."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    await engine.disable_global_maintenance(actor=_actor(request), platform=_platform(request))
    config = await engine.get_global_maintenance()
    return HTMLResponse(_render_global_widget(tpl, config, prefix))


def _render_service_widget(tpl: Jinja2Templates, config: object, service: str, prefix: str) -> str:
    """Render the per-service maintenance status widget partial."""
    return tpl.env.get_template("partials/service_maintenance.html").render(
        config=config,
        service=service,
        prefix=prefix,
    )


async def modal_service_enable(request: Request) -> HTMLResponse:
    """Return the per-service maintenance enable modal form."""
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")
    html = tpl.env.get_template("partials/modal_service_enable.html").render(
        prefix=prefix, service=service
    )
    return HTMLResponse(html)


async def modal_service_disable(request: Request) -> HTMLResponse:
    """Return the per-service maintenance disable confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")
    html = tpl.env.get_template("partials/modal_service_disable.html").render(
        prefix=prefix, service=service
    )
    return HTMLResponse(html)


async def service_maintenance_enable(request: Request) -> HTMLResponse:
    """Enable per-service maintenance from form data."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    form = await request.form()
    service = str(form.get("service", ""))
    reason = str(form.get("reason", ""))
    exempt_raw = str(form.get("exempt_paths", ""))
    exempt_paths = [p.strip() for p in exempt_raw.splitlines() if p.strip()]
    include_force_active = form.get("include_force_active") == "1"

    if not service:
        return HTMLResponse("Missing service", status_code=400)

    await engine.enable_service_maintenance(
        service=service,
        reason=reason,
        exempt_paths=exempt_paths,
        include_force_active=include_force_active,
        actor=_actor(request),
        platform=_platform(request),
    )
    config = await engine.get_service_maintenance(service)
    return HTMLResponse(_render_service_widget(tpl, config, service, prefix))


async def service_maintenance_disable(request: Request) -> HTMLResponse:
    """Disable per-service maintenance from form data."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    form = await request.form()
    service = str(form.get("service", ""))
    if not service:
        return HTMLResponse("Missing service", status_code=400)

    await engine.disable_service_maintenance(
        service=service,
        actor=_actor(request),
        platform=_platform(request),
    )
    config = await engine.get_service_maintenance(service)
    return HTMLResponse(_render_service_widget(tpl, config, service, prefix))


async def modal_env_gate(request: Request) -> HTMLResponse:
    """Return the env-gate modal form pre-filled with the current allowed envs."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    path_key = request.path_params["path_key"]
    route_path = _decode_path(path_key)
    slug = path_slug(route_path)

    try:
        state = await engine.get_state(route_path)
        current_envs = ", ".join(state.allowed_envs or [])
    except Exception:
        current_envs = ""

    html = tpl.env.get_template("partials/modal_env_gate.html").render(
        route_path=route_path,
        path_slug=slug,
        submit_path=f"{prefix}/env/{path_key}",
        current_envs=current_envs,
    )
    return HTMLResponse(html)


async def env_gate(request: Request) -> HTMLResponse:
    """Apply env-gating from form data and return the updated route row.

    Expected form fields: ``envs`` — comma-separated environment names.
    """
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])

    form_data = await request.form()
    raw = str(form_data.get("envs", ""))
    envs = [e.strip() for e in raw.replace(",", " ").split() if e.strip()]

    try:
        new_state = await engine.set_env_only(
            route_path, envs, actor=_actor(request), platform=_platform(request)
        )
    except RouteProtectedException:
        new_state = await engine.get_state(route_path)

    return HTMLResponse(_render_route_row(tpl, new_state, prefix))


async def action_modal(request: Request) -> HTMLResponse:
    """Return the styled action confirmation modal content.

    Renders ``partials/modal.html`` with action-specific copy and the form
    action URL pre-filled.  The modal is loaded into the ``<dialog>`` element
    via HTMX; the JS bootstrap in ``base.html`` calls ``showModal()`` after
    the swap.

    Parameters (URL path)
    ---------------------
    action:
        One of ``"enable"``, ``"maintenance"``, or ``"disable"``.
    path_key:
        Base64url-encoded route path key.
    """
    action = request.path_params["action"]
    path_key = request.path_params["path_key"]
    route_path = _decode_path(path_key)
    tpl = _templates(request)
    prefix = _prefix(request)

    action_map = {
        "enable": f"{prefix}/enable/{path_key}",
        "maintenance": f"{prefix}/toggle/{path_key}",
        "disable": f"{prefix}/disable/{path_key}",
    }
    submit_path = action_map.get(action, f"{prefix}/toggle/{path_key}")

    html = tpl.env.get_template("partials/modal.html").render(
        action=action,
        route_path=route_path,
        path_slug=path_slug(route_path),
        submit_path=submit_path,
        prefix=prefix,
    )
    return HTMLResponse(html)


async def rate_limits_page(request: Request) -> Response:
    """Render the rate limits page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    service = request.query_params.get("service", "")
    states = await engine.list_states()
    services = _get_services(states)
    svc_paths = {
        s.path[len(s.service) + 1 :] if s.service and s.path.startswith(s.service + ":") else s.path
        for s in states
        if not service or s.service == service
    }
    policies = list(engine._rate_limit_policies.values())
    if service:
        policies = [p for p in policies if p.path in svc_paths]
    paged = _paginate(policies, page)
    global_rl = await engine.get_global_rate_limit()
    service_rl = await engine.get_service_rate_limit(service) if service else None
    unrated_routes = _get_unrated_routes(states, engine._rate_limit_policies, service)
    return tpl.TemplateResponse(
        request,
        "rate_limits.html",
        {
            "policies": paged["items"],
            "pagination": paged,
            "global_rl": global_rl,
            "service_rl": service_rl,
            "prefix": prefix,
            "active_tab": "rate_limits",
            "version": request.app.state.version,
            "shield_actor": _actor(request),
            "services": services,
            "selected_service": service,
            "unrated_routes": unrated_routes,
        },
    )


async def rl_hits_page(request: Request) -> Response:
    """Render the blocked requests page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    service = request.query_params.get("service", "")
    states = await engine.list_states()
    services = _get_services(states)
    svc_paths = {
        s.path[len(s.service) + 1 :] if s.service and s.path.startswith(s.service + ":") else s.path
        for s in states
        if not service or s.service == service
    }
    hits = await engine.get_rate_limit_hits(limit=10_000)
    if service:
        hits = [h for h in hits if h.path in svc_paths]
    paged = _paginate(hits, page)
    return tpl.TemplateResponse(
        request,
        "rl_hits.html",
        {
            "hits": paged["items"],
            "pagination": paged,
            "prefix": prefix,
            "active_tab": "rl_hits",
            "version": request.app.state.version,
            "shield_actor": _actor(request),
            "services": services,
            "selected_service": service,
        },
    )


async def rate_limits_rows_partial(request: Request) -> Response:
    """Return only the rate limit policies table rows (HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    service = request.query_params.get("service", "")
    if service:
        states = await engine.list_states()
        svc_paths = {
            s.path[len(s.service) + 1 :]
            if s.service and s.path.startswith(f"{s.service}:")
            else s.path  # noqa: E501
            for s in states
            if s.service == service
        }
        policies = [p for p in engine._rate_limit_policies.values() if p.path in svc_paths]
    else:
        policies = list(engine._rate_limit_policies.values())
    paged = _paginate(policies, page)
    return tpl.TemplateResponse(
        request,
        "partials/rate_limit_rows.html",
        {"policies": paged["items"], "prefix": prefix, "selected_service": service},
    )


def _render_rl_row(tpl: Jinja2Templates, policy: Any, prefix: str) -> str:
    """Render the rate_limit_rows.html partial for a single policy.

    Appends a tiny inline script that closes the edit modal so the modal
    close fires only on a successful save (not on validation errors).
    """
    html = tpl.env.get_template("partials/rate_limit_rows.html").render(
        policies=[policy],
        prefix=prefix,
    )
    return html + "<script>document.getElementById('shield-modal').close()</script>"


# ------------------------------------------------------------------
# Rate limit modal GET handlers
# ------------------------------------------------------------------


async def modal_rl_reset(request: Request) -> HTMLResponse:
    """Return the reset-counters confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    slug = path_slug(composite)
    html = tpl.env.get_template("partials/modal_rl_reset.html").render(
        method=method,
        route_path=route_path,
        path_slug=slug,
        submit_path=f"{prefix}/rl/reset/{request.path_params['path_key']}",
    )
    return HTMLResponse(html)


async def modal_rl_edit(request: Request) -> HTMLResponse:
    """Return the edit-policy modal pre-filled with current values."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    slug = path_slug(composite)
    policy = engine._rate_limit_policies.get(composite)
    html = tpl.env.get_template("partials/modal_rl_edit.html").render(
        method=method,
        route_path=route_path,
        path_slug=slug,
        submit_path=f"{prefix}/rl/edit/{request.path_params['path_key']}",
        current_limit=policy.limit if policy else "",
        current_algorithm=policy.algorithm if policy else "sliding_window",
        current_key_strategy=policy.key_strategy if policy else "ip",
    )
    return HTMLResponse(html)


async def modal_rl_add(request: Request) -> HTMLResponse:
    """Return the add-policy modal for a route that has no rate limit yet."""
    tpl = _templates(request)
    prefix = _prefix(request)
    route_path = _decode_path(request.path_params["path_key"])
    selected_service = request.query_params.get("service", "")

    # Extract the HTTP method prefix (e.g. "GET:/api/pay" → method="GET", path="/api/pay").
    _http_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    if ":" in route_path:
        candidate, _, bare_path = route_path.partition(":")
        if candidate.upper() in _http_methods:
            method = candidate.upper()
        else:
            method = ""
            bare_path = route_path
    else:
        method = ""
        bare_path = route_path

    html = tpl.env.get_template("partials/modal_rl_add.html").render(
        route_path=bare_path,
        route_method=method,
        prefix=prefix,
        selected_service=selected_service,
    )
    return HTMLResponse(html)


async def rl_add(request: Request) -> Response:
    """POST /rl/add — create a new rate limit policy from form data.

    Reads ``path``, ``method``, ``limit``, ``algorithm``, ``key_strategy``,
    and ``burst`` from the form body, registers the policy, then triggers
    an HTMX page redirect so both the policies table and unrated list refresh.
    """
    engine = _engine(request)
    prefix = _prefix(request)
    form = await request.form()
    path = str(form.get("path", "")).strip()
    method = str(form.get("method", "GET")).strip().upper() or "GET"
    limit = str(form.get("limit", "")).strip()
    algorithm = str(form.get("algorithm", "sliding_window")).strip() or None
    key_strategy = str(form.get("key_strategy", "ip")).strip() or None
    burst = int(str(form.get("burst", 0) or 0))
    service = str(form.get("service", "")).strip()

    if path and limit:
        try:
            await engine.set_rate_limit_policy(
                path=path,
                method=method,
                limit=limit,
                algorithm=algorithm,
                key_strategy=key_strategy,
                burst=burst,
                actor=_actor(request),
                platform=_platform(request),
            )
        except ValueError as exc:
            tpl = _templates(request)
            html = tpl.env.get_template("partials/modal_rl_add.html").render(
                route_path=path,
                route_method=method,
                prefix=prefix,
                selected_service=service,
                error=str(exc),
                limit_value=limit,
                algorithm_value=algorithm,
                key_strategy_value=key_strategy,
            )
            return HTMLResponse(html)

    qs = f"?service={service}" if service else ""
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"{prefix}/rate-limits{qs}"},
    )


async def modal_rl_delete(request: Request) -> HTMLResponse:
    """Return the delete-policy confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    slug = path_slug(composite)
    html = tpl.env.get_template("partials/modal_rl_delete.html").render(
        method=method,
        route_path=route_path,
        path_slug=slug,
        submit_path=f"{prefix}/rl/delete/{request.path_params['path_key']}",
    )
    return HTMLResponse(html)


# ------------------------------------------------------------------
# Rate limit action POST handlers
# ------------------------------------------------------------------


async def rl_reset(request: Request) -> HTMLResponse:
    """Reset counters for the policy and return the unchanged row."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    await engine.reset_rate_limit(
        route_path, method=method, actor=_actor(request), platform=_platform(request)
    )
    policy = engine._rate_limit_policies.get(composite)
    if policy is None:
        return HTMLResponse("")
    return HTMLResponse(_render_rl_row(tpl, policy, prefix))


async def rl_edit(request: Request) -> HTMLResponse:
    """Update the policy from form data and return the refreshed row."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    form = await request.form()
    limit = str(form.get("limit", "")).strip()
    algorithm = str(form.get("algorithm", "sliding_window")).strip()
    key_strategy = str(form.get("key_strategy", "ip")).strip()
    if not limit:
        policy = engine._rate_limit_policies.get(composite)
        if policy is None:
            return HTMLResponse("")
        return HTMLResponse(_render_rl_row(tpl, policy, prefix))
    try:
        await engine.set_rate_limit_policy(
            route_path,
            method,
            limit,
            algorithm=algorithm,
            key_strategy=key_strategy,
            actor=_actor(request),
            platform=_platform(request),
        )
    except ValueError as exc:
        slug = path_slug(composite)
        html = tpl.env.get_template("partials/modal_rl_edit.html").render(
            method=method,
            route_path=route_path,
            path_slug=slug,
            submit_path=f"{prefix}/rl/edit/{request.path_params['path_key']}",
            current_limit=limit,
            current_algorithm=algorithm,
            current_key_strategy=key_strategy,
            error=str(exc),
        )
        return HTMLResponse(
            html,
            headers={"HX-Retarget": "#shield-modal", "HX-Reswap": "innerHTML"},
        )
    policy = engine._rate_limit_policies.get(composite)
    if policy is None:
        return HTMLResponse("")
    return HTMLResponse(_render_rl_row(tpl, policy, prefix))


async def rl_delete(request: Request) -> HTMLResponse:
    """Delete the persisted policy and remove the row."""
    engine = _engine(request)
    composite = _decode_path(request.path_params["path_key"])
    method, _, route_path = composite.partition(":")
    await engine.delete_rate_limit_policy(
        route_path, method, actor=_actor(request), platform=_platform(request)
    )
    # Return an empty string — HTMX outerHTML-swaps the row away.
    return HTMLResponse("")


async def modal_global_rl(request: Request) -> HTMLResponse:
    """Return the global rate limit set/edit modal form."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/modal_global_rl.html").render(
        grl=grl,
        prefix=prefix,
    )
    return HTMLResponse(html)


async def modal_global_rl_delete(request: Request) -> HTMLResponse:
    """Return the global rate limit delete confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    html = tpl.env.get_template("partials/modal_global_rl_delete.html").render(prefix=prefix)
    return HTMLResponse(html)


async def modal_global_rl_reset(request: Request) -> HTMLResponse:
    """Return the global rate limit reset confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    html = tpl.env.get_template("partials/modal_global_rl_reset.html").render(prefix=prefix)
    return HTMLResponse(html)


async def global_rl_set(request: Request) -> HTMLResponse:
    """Save global rate limit policy from form data and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    limit = str(form.get("limit", "")).strip()
    algorithm = str(form.get("algorithm", "fixed_window")).strip() or None
    key_strategy = str(form.get("key_strategy", "ip")).strip() or None
    burst = int(str(form.get("burst", 0) or 0))
    exempt_raw = str(form.get("exempt_routes", "")).strip()
    exempt_routes = [r.strip() for r in exempt_raw.splitlines() if r.strip()]
    if limit:
        await engine.set_global_rate_limit(
            limit=limit,
            algorithm=algorithm,
            key_strategy=key_strategy,
            burst=burst,
            exempt_routes=exempt_routes,
            actor=_actor(request),
            platform=_platform(request),
        )
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/global_rl_card.html").render(grl=grl, prefix=prefix)
    return HTMLResponse(html)


async def global_rl_delete(request: Request) -> HTMLResponse:
    """Delete global rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    await engine.delete_global_rate_limit(actor=_actor(request), platform=_platform(request))
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/global_rl_card.html").render(grl=grl, prefix=prefix)
    return HTMLResponse(html)


async def global_rl_reset(request: Request) -> HTMLResponse:
    """Reset global rate limit counters and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    await engine.reset_global_rate_limit(actor=_actor(request), platform=_platform(request))
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/global_rl_card.html").render(grl=grl, prefix=prefix)
    return HTMLResponse(html)


async def global_rl_enable(request: Request) -> HTMLResponse:
    """Enable (resume) the global rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    await engine.enable_global_rate_limit(actor=_actor(request), platform=_platform(request))
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/global_rl_card.html").render(grl=grl, prefix=prefix)
    return HTMLResponse(html)


async def global_rl_disable(request: Request) -> HTMLResponse:
    """Disable (pause) the global rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    await engine.disable_global_rate_limit(actor=_actor(request), platform=_platform(request))
    grl = await engine.get_global_rate_limit()
    html = tpl.env.get_template("partials/global_rl_card.html").render(grl=grl, prefix=prefix)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Per-service rate limit dashboard handlers
# ---------------------------------------------------------------------------


async def modal_service_rl(request: Request) -> HTMLResponse:
    """Return the per-service rate limit set/edit modal form."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/modal_service_rl.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def modal_service_rl_delete(request: Request) -> HTMLResponse:
    """Return the per-service rate limit delete confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")
    html = tpl.env.get_template("partials/modal_service_rl_delete.html").render(
        service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def modal_service_rl_reset(request: Request) -> HTMLResponse:
    """Return the per-service rate limit reset confirmation modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")
    html = tpl.env.get_template("partials/modal_service_rl_reset.html").render(
        service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def service_rl_set(request: Request) -> HTMLResponse:
    """Save per-service rate limit policy from form data and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    service = str(form.get("service", "")).strip()
    limit = str(form.get("limit", "")).strip()
    algorithm = str(form.get("algorithm", "fixed_window")).strip() or None
    key_strategy = str(form.get("key_strategy", "ip")).strip() or None
    burst = int(str(form.get("burst", 0) or 0))
    exempt_raw = str(form.get("exempt_routes", "")).strip()
    exempt_routes = [r.strip() for r in exempt_raw.splitlines() if r.strip()]
    if limit and service:
        try:
            await engine.set_service_rate_limit(
                service,
                limit=limit,
                algorithm=algorithm,
                key_strategy=key_strategy,
                burst=burst,
                exempt_routes=exempt_routes,
                actor=_actor(request),
                platform=_platform(request),
            )
        except Exception:
            pass
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/service_rl_card.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def service_rl_delete(request: Request) -> HTMLResponse:
    """Delete per-service rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    service = str(form.get("service", "")).strip()
    if service:
        await engine.delete_service_rate_limit(
            service, actor=_actor(request), platform=_platform(request)
        )
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/service_rl_card.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def service_rl_reset(request: Request) -> HTMLResponse:
    """Reset per-service rate limit counters and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    service = str(form.get("service", "")).strip()
    if service:
        await engine.reset_service_rate_limit(
            service, actor=_actor(request), platform=_platform(request)
        )
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/service_rl_card.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def service_rl_enable(request: Request) -> HTMLResponse:
    """Enable (resume) per-service rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    service = str(form.get("service", "")).strip()
    if service:
        await engine.enable_service_rate_limit(
            service, actor=_actor(request), platform=_platform(request)
        )
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/service_rl_card.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def service_rl_disable(request: Request) -> HTMLResponse:
    """Disable (pause) per-service rate limit policy and refresh the card."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    form = await request.form()
    service = str(form.get("service", "")).strip()
    if service:
        await engine.disable_service_rate_limit(
            service, actor=_actor(request), platform=_platform(request)
        )
    srl = await engine.get_service_rate_limit(service)
    html = tpl.env.get_template("partials/service_rl_card.html").render(
        srl=srl, service=service, prefix=prefix
    )
    return HTMLResponse(html)


async def rate_limits_hits_partial(request: Request) -> Response:
    """Return only the recent blocked requests table rows (HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)

    service = request.query_params.get("service", "")
    hits = await engine.get_rate_limit_hits(limit=50)
    if service:
        states = await engine.list_states()
        svc_paths = {
            s.path[len(s.service) + 1 :]
            if s.service and s.path.startswith(f"{s.service}:")
            else s.path  # noqa: E501
            for s in states
            if s.service == service
        }
        hits = [h for h in hits if h.path in svc_paths]
    return tpl.TemplateResponse(
        request,
        "partials/rate_limit_hits.html",
        {"hits": hits, "selected_service": service},
    )


async def events(request: Request) -> StreamingResponse:
    """SSE endpoint that streams live route state changes.

    When the backend supports ``subscribe()`` (e.g. ``MemoryBackend``),
    each state change is pushed to connected clients as an SSE event named
    ``shield:update:{path_slug}``.  HTMX receives the event and replaces
    the matching ``<tr>`` via ``sse-swap``.

    When the backend does **not** support ``subscribe()`` (e.g.
    ``FileBackend``), a ``NotImplementedError`` is raised on the first
    iteration.  In that case the endpoint falls back to sending a
    keepalive comment every 15 seconds so the browser connection stays
    open without errors.

    Keepalive comments (``": keepalive\\n\\n"``) are valid SSE syntax that
    browsers silently ignore.
    """
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)
    service = request.query_params.get("service", "")

    async def _generate() -> object:
        try:
            async for state in engine.backend.subscribe():
                if service and state.service != service:
                    continue
                slug = path_slug(state.path)
                html = _render_route_row(tpl, state, prefix)
                # Format as multi-line SSE data — each HTML line prefixed with "data: ".
                data_lines = "\ndata: ".join(html.splitlines())
                yield f"event: shield:update:{slug}\ndata: {data_lines}\n\n"
        except NotImplementedError:
            # Backend does not support pub/sub — fall through to keepalive loop.
            pass
        except Exception:
            logger.exception("shield dashboard: SSE subscription error, falling back to keepalive")

        # Keepalive ping loop — runs when subscribe() is unsupported OR after
        # the subscription ends.  Browsers keep the connection alive.
        while True:
            if await request.is_disconnected():
                break
            yield ": keepalive\n\n"
            try:
                await anyio.sleep(15)
            except Exception:
                break

    return StreamingResponse(
        _generate(),  # type: ignore[arg-type]
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Feature flag dashboard pages
# ---------------------------------------------------------------------------

_FLAG_TYPE_COLOURS = {
    "boolean": "emerald",
    "string": "blue",
    "integer": "violet",
    "float": "violet",
    "json": "amber",
}


async def flags_page(request: Request) -> Response:
    """GET /flags — feature flag list page."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    flags = await engine.list_flags()
    return tpl.TemplateResponse(
        request,
        "flags.html",
        {
            "prefix": prefix,
            "flags": flags,
            "active_tab": "flags",
            "shield_actor": _actor(request),
            "version": request.app.state.version,
            "flag_type_colours": _FLAG_TYPE_COLOURS,
            "flags_enabled": True,
        },
    )


async def flags_rows_partial(request: Request) -> Response:
    """GET /flags/rows — HTMX partial: flag table rows only.

    Supports ``?q=`` search query and ``?type=`` / ``?status=`` filters.
    """
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    flags = await engine.list_flags()

    q = request.query_params.get("q", "").lower().strip()
    ftype = request.query_params.get("type", "").strip()
    status_filter = request.query_params.get("status", "").strip()

    if q:
        flags = [f for f in flags if q in f.key.lower() or q in (f.name or "").lower()]
    if ftype:
        flags = [f for f in flags if f.type.value == ftype]
    if status_filter == "enabled":
        flags = [f for f in flags if f.enabled]
    elif status_filter == "disabled":
        flags = [f for f in flags if not f.enabled]

    return tpl.TemplateResponse(
        request,
        "partials/flag_rows.html",
        {
            "prefix": prefix,
            "flags": flags,
            "flag_type_colours": _FLAG_TYPE_COLOURS,
        },
    )


async def flag_detail_page(request: Request) -> Response:
    """GET /flags/{key} — single flag detail page."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p>Flag not found.</p>", status_code=404)
    segments = await engine.list_segments()
    all_flags = await engine.list_flags()
    return tpl.TemplateResponse(
        request,
        "flag_detail.html",
        {
            "prefix": prefix,
            "flag": flag,
            "segments": segments,
            "all_flags": [f for f in all_flags if f.key != key],
            "active_tab": "flags",
            "shield_actor": _actor(request),
            "version": request.app.state.version,
            "flag_type_colours": _FLAG_TYPE_COLOURS,
            "flags_enabled": True,
        },
    )


async def flag_enable(request: Request) -> Response:
    """POST /flags/{key}/enable — enable a flag; return updated row partial."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<tr><td>Flag not found</td></tr>", status_code=404)
    flag = flag.model_copy(update={"enabled": True})
    await engine.save_flag(
        flag, actor=_actor(request), platform=_platform(request), action="flag_enabled"
    )
    return tpl.TemplateResponse(
        request,
        "partials/flag_row.html",
        {"prefix": prefix, "flag": flag, "flag_type_colours": _FLAG_TYPE_COLOURS},
    )


async def flag_disable(request: Request) -> Response:
    """POST /flags/{key}/disable — disable a flag; return updated row partial."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<tr><td>Flag not found</td></tr>", status_code=404)
    flag = flag.model_copy(update={"enabled": False})
    await engine.save_flag(
        flag, actor=_actor(request), platform=_platform(request), action="flag_disabled"
    )
    return tpl.TemplateResponse(
        request,
        "partials/flag_row.html",
        {"prefix": prefix, "flag": flag, "flag_type_colours": _FLAG_TYPE_COLOURS},
    )


async def flag_delete(request: Request) -> Response:
    """DELETE /flags/{key} — delete a flag; return empty response (HTMX removes row)."""
    engine = _engine(request)
    key = request.path_params["key"]
    await engine.delete_flag(key, actor=_actor(request), platform=_platform(request))
    return HTMLResponse("")


async def modal_flag_create(request: Request) -> Response:
    """GET /modal/flag/create — return create flag modal HTML."""
    tpl = _templates(request)
    prefix = _prefix(request)
    return tpl.TemplateResponse(
        request,
        "partials/modal_flag_create.html",
        {"prefix": prefix},
    )


async def flag_create_form(request: Request) -> Response:
    """POST /flags/create — create a flag from form data; return new row partial."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    form = await request.form()
    key = str(form.get("key", "")).strip()
    name = str(form.get("name", "")).strip()
    ftype = str(form.get("type", "boolean")).strip()

    if not key or not name:
        return HTMLResponse(
            "<p class='text-red-600 text-sm p-3'>Key and name are required.</p>",
            status_code=400,
        )

    from shield.core.feature_flags.models import FeatureFlag, FlagType, FlagVariation

    type_map = {
        "boolean": (
            FlagType.BOOLEAN,
            [FlagVariation(name="on", value=True), FlagVariation(name="off", value=False)],
            "off",
            "off",
        ),
        "string": (
            FlagType.STRING,
            [
                FlagVariation(name="control", value="control"),
                FlagVariation(name="treatment", value="treatment"),
            ],
            "control",
            "control",
        ),
        "integer": (
            FlagType.INTEGER,
            [FlagVariation(name="off", value=0), FlagVariation(name="on", value=1)],
            "off",
            "off",
        ),
        "float": (
            FlagType.FLOAT,
            [FlagVariation(name="off", value=0.0), FlagVariation(name="on", value=1.0)],
            "off",
            "off",
        ),
        "json": (
            FlagType.JSON,
            [FlagVariation(name="off", value={}), FlagVariation(name="on", value={})],
            "off",
            "off",
        ),
    }
    if ftype not in type_map:
        ftype = "boolean"
    ft, variations, off_var, fallthrough = type_map[ftype]

    flag = FeatureFlag(
        key=key,
        name=name,
        type=ft,
        variations=variations,
        off_variation=off_var,
        fallthrough=fallthrough,
        enabled=True,
    )
    await engine.save_flag(flag, actor=_actor(request), platform=_platform(request))
    return tpl.TemplateResponse(
        request,
        "partials/flag_row.html",
        {"prefix": prefix, "flag": flag, "flag_type_colours": _FLAG_TYPE_COLOURS},
        headers={"HX-Trigger": "flagCreated"},
    )


async def modal_flag_eval(request: Request) -> Response:
    """GET /modal/flag/{key}/eval — return eval debugger modal HTML."""
    tpl = _templates(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    engine = _engine(request)
    flag = await engine.get_flag(key)
    return tpl.TemplateResponse(
        request,
        "partials/modal_flag_eval.html",
        {"prefix": prefix, "flag": flag, "key": key},
    )


async def flag_eval_form(request: Request) -> Response:
    """POST /flags/{key}/eval — evaluate flag from form data; return rich result partial."""
    import json as _json

    from shield.core.feature_flags.evaluator import FlagEvaluator
    from shield.core.feature_flags.models import EvaluationContext

    tpl = _templates(request)
    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)

    form = await request.form()
    ctx_key = str(form.get("context_key", "anonymous")).strip() or "anonymous"
    kind = str(form.get("kind", "user")).strip() or "user"
    attrs_raw = str(form.get("attributes", "")).strip()
    attributes: dict[str, str] = {}
    for line in attrs_raw.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            attributes[k.strip()] = v.strip()

    ctx = EvaluationContext(key=ctx_key, kind=kind, attributes=attributes)
    all_flags_list = await engine.list_flags()
    all_flags = {f.key: f for f in all_flags_list}
    segments_list = await engine.list_segments()
    segments = {s.key: s for s in segments_list}
    evaluator = FlagEvaluator(segments=segments)
    result = evaluator.evaluate(flag, ctx, all_flags)

    # Look up rule description for RULE_MATCH
    rule_description = ""
    if result.rule_id:
        for rule in flag.rules:
            if rule.id == result.rule_id:
                rule_description = rule.description or ""
                break

    # Serialize value as JSON for display (handles bool, dict, list, etc.)
    try:
        value_json = _json.dumps(result.value)
    except (TypeError, ValueError):
        value_json = str(result.value)

    trigger = _json.dumps(
        {
            "shieldEvalDone": {
                "flagKey": key,
                "value": result.value,
                "reason": result.reason.value,
                "error": bool(result.error_message),
                "errorMessage": result.error_message or "",
            }
        }
    )
    return tpl.TemplateResponse(
        request,
        "partials/flag_eval_result.html",
        {
            "result": result,
            "rule_description": rule_description,
            "value_json": value_json,
            "ctx_key": ctx_key,
            "ctx_kind": kind,
            "ctx_attributes": attributes,
        },
        headers={"HX-Trigger": trigger},
    )


async def flag_settings_save(request: Request) -> Response:
    """POST /flags/{key}/settings/save — update flag name and description."""
    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)
    form = await request.form()
    name = str(form.get("name", flag.name)).strip() or flag.name
    description = str(form.get("description", flag.description or "")).strip()
    updated = flag.model_copy(update={"name": name, "description": description})
    await engine.save_flag(updated, actor=_actor(request), platform=_platform(request))
    _svg = (
        "<svg class='w-4 h-4' fill='none' viewBox='0 0 24 24'"
        " stroke='currentColor' stroke-width='2.5'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/></svg>"
    )
    return HTMLResponse(
        "<div class='flex items-center gap-2 text-sm text-emerald-600 font-medium'>"
        + _svg
        + "Settings saved</div>",
        headers={"HX-Trigger": '{"flagSettingsSaved": true}'},
    )


async def flag_variations_save(request: Request) -> Response:
    """POST /flags/{key}/variations/save — replace flag variations."""
    import json as _json
    import re as _re

    from shield.core.feature_flags.models import FlagType, FlagVariation

    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)

    form = await request.form()
    # Parse variations[N][field] pattern
    indices: dict[int, dict[str, str]] = {}
    for k, v in form.multi_items():
        m = _re.match(r"variations\[(\d+)\]\[(\w+)\]", k)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            indices.setdefault(idx, {})[field] = str(v)

    if not indices:
        return HTMLResponse(
            "<p class='text-red-600 text-sm'>No variations provided.</p>",
            status_code=400,
        )

    flag_type = flag.type
    variations = []
    for i in sorted(indices.keys()):
        entry = indices[i]
        if entry.get("_deleted") == "1":
            continue
        name = entry.get("name", "").strip()
        if not name:
            return HTMLResponse(
                f"<p class='text-red-600 text-sm'>Variation {i} has no name.</p>",
                status_code=400,
            )
        raw_val = entry.get("value", "")
        try:
            parsed_val: bool | int | float | str | dict[str, Any] | list[Any]
            if flag_type == FlagType.BOOLEAN:
                parsed_val = raw_val.lower() in ("true", "1", "yes", "on")
            elif flag_type == FlagType.INTEGER:
                parsed_val = int(raw_val)
            elif flag_type == FlagType.FLOAT:
                parsed_val = float(raw_val)
            elif flag_type == FlagType.JSON:
                parsed_val = _json.loads(raw_val) if raw_val.strip() else {}
            else:
                parsed_val = raw_val
            val = parsed_val
        except Exception:
            return HTMLResponse(
                f"<p class='text-red-600 text-sm'>Invalid value for variation '{name}'.</p>",
                status_code=400,
            )
        variations.append(
            FlagVariation(name=name, value=val, description=entry.get("description", "") or "")
        )

    if len(variations) < 2:
        return HTMLResponse(
            "<p class='text-red-600 text-sm'>At least two variations required.</p>",
            status_code=400,
        )

    variation_names = {v.name for v in variations}
    patch: dict[str, Any] = {"variations": variations}
    # Fix off_variation if it no longer exists
    if flag.off_variation not in variation_names:
        patch["off_variation"] = variations[0].name
    # Fix fallthrough if string and no longer valid
    if isinstance(flag.fallthrough, str) and flag.fallthrough not in variation_names:
        patch["fallthrough"] = variations[0].name

    updated = flag.model_copy(update=patch)
    await engine.save_flag(updated, actor=_actor(request), platform=_platform(request))
    _svg = (
        "<svg class='w-4 h-4' fill='none' viewBox='0 0 24 24'"
        " stroke='currentColor' stroke-width='2.5'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/></svg>"
    )
    return HTMLResponse(
        "<div class='flex items-center gap-2 text-sm text-emerald-600 font-medium'>"
        + _svg
        + "Variations saved</div>",
        headers={"HX-Trigger": '{"flagVariationsSaved": true}'},
    )


async def flag_targeting_save(request: Request) -> Response:
    """POST /flags/{key}/targeting/save — update off_variation, fallthrough, and rules."""
    import re as _re
    import uuid as _uuid

    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)

    form = await request.form()
    variation_names = {v.name for v in flag.variations}

    patch: dict[str, Any] = {}

    # off_variation
    off_var = str(form.get("off_variation", "")).strip()
    if off_var:
        if off_var not in variation_names:
            return HTMLResponse(
                f"<p class='text-red-600 text-sm'>Unknown variation: {off_var}</p>",
                status_code=400,
            )
        patch["off_variation"] = off_var

    # fallthrough (only simple string form supported in dashboard)
    fallthrough = str(form.get("fallthrough", "")).strip()
    if fallthrough:
        if fallthrough not in variation_names:
            return HTMLResponse(
                f"<p class='text-red-600 text-sm'>Unknown variation: {fallthrough}</p>",
                status_code=400,
            )
        patch["fallthrough"] = fallthrough

    # rules — parse rules[N][field] and rules[N][clauses][M][field]
    rule_data: dict[int, dict[str, Any]] = {}
    for k, v in form.multi_items():
        m = _re.match(r"rules\[(\d+)\]\[clauses\]\[(\d+)\]\[(\w+)\]", k)
        if m:
            ri, ci, field = int(m.group(1)), int(m.group(2)), m.group(3)
            rule_data.setdefault(ri, {}).setdefault("_clauses", {}).setdefault(ci, {})[field] = str(
                v
            )
            continue
        m = _re.match(r"rules\[(\d+)\]\[(\w+)\]", k)
        if m:
            ri, field = int(m.group(1)), m.group(2)
            rule_data.setdefault(ri, {})[field] = str(v)

    if rule_data:
        from shield.core.feature_flags.models import Operator, RuleClause, TargetingRule

        rules = []
        for ri in sorted(rule_data.keys()):
            rd = rule_data[ri]
            if rd.get("_deleted") == "1":
                continue
            variation = rd.get("variation", "").strip()
            if variation and variation not in variation_names:
                return HTMLResponse(
                    f"<p class='text-red-600 text-sm'>"
                    f"Rule {ri}: unknown variation '{variation}'</p>",
                    status_code=400,
                )
            rule_id = rd.get("id", "").strip() or str(_uuid.uuid4())
            clauses = []
            for ci in sorted(rd.get("_clauses", {}).keys()):
                cd = rd["_clauses"][ci]
                if cd.get("_deleted") == "1":
                    continue
                op_str = cd.get("operator", "is").strip()
                try:
                    op = Operator(op_str)
                except ValueError:
                    op = Operator.IS
                # For segment operators the attribute field is hidden — default to "key"
                is_seg_op = op in (Operator.IN_SEGMENT, Operator.NOT_IN_SEGMENT)
                attr = cd.get("attribute", "").strip() or ("key" if is_seg_op else "")
                if not attr:
                    continue
                raw_values = cd.get("values", "")
                values = [v.strip() for v in raw_values.split(",") if v.strip()]
                negate = cd.get("negate", "false").lower() == "true"
                clauses.append(
                    RuleClause(attribute=attr, operator=op, values=values, negate=negate)
                )
            rules.append(
                TargetingRule(
                    id=rule_id,
                    description=rd.get("description", "") or "",
                    clauses=clauses,
                    variation=variation or None,
                )
            )
        patch["rules"] = rules

    if not patch:
        return HTMLResponse(
            "<p class='text-amber-600 text-sm'>Nothing to save.</p>", status_code=200
        )

    updated = flag.model_copy(update=patch)
    await engine.save_flag(updated, actor=_actor(request), platform=_platform(request))
    _svg = (
        "<svg class='w-4 h-4' fill='none' viewBox='0 0 24 24'"
        " stroke='currentColor' stroke-width='2.5'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/></svg>"
    )
    return HTMLResponse(
        "<div class='flex items-center gap-2 text-sm text-emerald-600 font-medium'>"
        + _svg
        + "Targeting saved</div>",
        headers={"HX-Trigger": '{"flagTargetingSaved": true}'},
    )


async def flag_prerequisites_save(request: Request) -> Response:
    """POST /flags/{key}/prerequisites/save — update flag prerequisites."""
    import re as _re

    from shield.core.feature_flags.models import Prerequisite

    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)

    form = await request.form()
    prereq_data: dict[int, dict[str, str]] = {}
    for k, v in form.multi_items():
        m = _re.match(r"prereqs\[(\d+)\]\[(\w+)\]", k)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            prereq_data.setdefault(idx, {})[field] = str(v)

    prereqs = []
    for i in sorted(prereq_data.keys()):
        entry = prereq_data[i]
        flag_key = entry.get("flag_key", "").strip()
        variation = entry.get("variation", "").strip()
        if not flag_key or not variation:
            continue
        if flag_key == key:
            return HTMLResponse(
                "<p class='text-red-600 text-sm'>A flag cannot be its own prerequisite.</p>",
                status_code=400,
            )
        prereqs.append(Prerequisite(flag_key=flag_key, variation=variation))

    updated = flag.model_copy(update={"prerequisites": prereqs})
    await engine.save_flag(updated, actor=_actor(request), platform=_platform(request))
    _svg = (
        "<svg class='w-4 h-4' fill='none' viewBox='0 0 24 24'"
        " stroke='currentColor' stroke-width='2.5'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/></svg>"
    )
    return HTMLResponse(
        "<div class='flex items-center gap-2 text-sm text-emerald-600 font-medium'>"
        + _svg
        + "Prerequisites saved</div>",
        headers={"HX-Trigger": '{"flagPrerequisitesSaved": true}'},
    )


async def flag_targets_save(request: Request) -> Response:
    """POST /flags/{key}/targets/save — update individual targets."""
    engine = _engine(request)
    key = request.path_params["key"]
    flag = await engine.get_flag(key)
    if flag is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Flag not found.</p>", status_code=404)

    form = await request.form()
    variation_names = {v.name for v in flag.variations}
    targets: dict[str, list[str]] = {}

    for k, v in form.multi_items():
        if k.startswith("targets[") and k.endswith("]"):
            variation_name = k[len("targets[") : -1]
            if variation_name not in variation_names:
                continue
            keys = [line.strip() for line in str(v).splitlines() if line.strip()]
            if keys:
                targets[variation_name] = keys

    updated = flag.model_copy(update={"targets": targets})
    await engine.save_flag(updated, actor=_actor(request), platform=_platform(request))
    _svg = (
        "<svg class='w-4 h-4' fill='none' viewBox='0 0 24 24'"
        " stroke='currentColor' stroke-width='2.5'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M5 13l4 4L19 7'/></svg>"
    )
    return HTMLResponse(
        "<div class='flex items-center gap-2 text-sm text-emerald-600 font-medium'>"
        + _svg
        + "Targets saved</div>",
        headers={"HX-Trigger": '{"flagTargetsSaved": true}'},
    )


# ---------------------------------------------------------------------------
# Segment dashboard pages
# ---------------------------------------------------------------------------


async def segments_page(request: Request) -> Response:
    """GET /segments — segment list page."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    segments = await engine.list_segments()
    return tpl.TemplateResponse(
        request,
        "segments.html",
        {
            "prefix": prefix,
            "segments": segments,
            "active_tab": "segments",
            "shield_actor": _actor(request),
            "version": request.app.state.version,
            "flags_enabled": True,
        },
    )


async def segments_rows_partial(request: Request) -> Response:
    """GET /segments/rows — HTMX partial: segment table rows only."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    segments = await engine.list_segments()
    q = request.query_params.get("q", "").lower().strip()
    if q:
        segments = [s for s in segments if q in s.key.lower() or q in (s.name or "").lower()]
    return tpl.TemplateResponse(
        request,
        "partials/segment_rows.html",
        {"prefix": prefix, "segments": segments},
    )


async def modal_segment_view(request: Request) -> Response:
    """GET /modal/segment/{key}/view — return segment info (read-only) modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    engine = _engine(request)
    key = request.path_params["key"]
    segment = await engine.get_segment(key)
    return tpl.TemplateResponse(
        request,
        "partials/modal_segment_view.html",
        {"prefix": prefix, "segment": segment, "key": key},
    )


async def modal_segment_detail(request: Request) -> Response:
    """GET /modal/segment/{key} — return segment detail/edit modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    engine = _engine(request)
    key = request.path_params["key"]
    segment = await engine.get_segment(key)
    return tpl.TemplateResponse(
        request,
        "partials/modal_segment_detail.html",
        {"prefix": prefix, "segment": segment, "key": key},
    )


async def modal_segment_create(request: Request) -> Response:
    """GET /modal/segment/create — return create segment modal."""
    tpl = _templates(request)
    prefix = _prefix(request)
    return tpl.TemplateResponse(
        request,
        "partials/modal_segment_create.html",
        {"prefix": prefix},
    )


async def segment_create_form(request: Request) -> Response:
    """POST /segments/create — create segment from form; return new row partial."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    form = await request.form()
    key = str(form.get("key", "")).strip()
    name = str(form.get("name", "")).strip()
    if not key or not name:
        return HTMLResponse(
            "<p class='text-red-600 text-sm p-3'>Key and name are required.</p>",
            status_code=400,
        )
    from shield.core.feature_flags.models import Segment

    segment = Segment(key=key, name=name)
    await engine.save_segment(segment, actor=_actor(request), platform=_platform(request))
    return tpl.TemplateResponse(
        request,
        "partials/segment_row.html",
        {"prefix": prefix, "segment": segment},
        headers={"HX-Trigger": "segmentCreated"},
    )


async def segment_delete(request: Request) -> Response:
    """DELETE /segments/{key} — delete segment; return empty (HTMX removes row)."""
    engine = _engine(request)
    key = request.path_params["key"]
    await engine.delete_segment(key, actor=_actor(request), platform=_platform(request))
    return HTMLResponse("")


async def segment_save_form(request: Request) -> Response:
    """POST /segments/{key}/save — save segment edits from detail modal."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    segment = await engine.get_segment(key)
    if segment is None:
        return HTMLResponse("<p class='text-red-600 text-sm'>Segment not found.</p>", 404)

    form = await request.form()
    # Parse included/excluded as newline-separated keys
    included_raw = str(form.get("included", "")).strip()
    excluded_raw = str(form.get("excluded", "")).strip()
    included = [k.strip() for k in included_raw.splitlines() if k.strip()]
    excluded = [k.strip() for k in excluded_raw.splitlines() if k.strip()]
    segment = segment.model_copy(update={"included": included, "excluded": excluded})
    await engine.save_segment(segment, actor=_actor(request), platform=_platform(request))
    return tpl.TemplateResponse(
        request,
        "partials/segment_row.html",
        {"prefix": prefix, "segment": segment},
        headers={"HX-Trigger": "segmentSaved"},
    )


async def segment_rule_add(request: Request) -> Response:
    """POST /segments/{key}/rules/add — add a targeting rule via the dashboard modal."""
    import uuid as _uuid

    from shield.core.feature_flags.models import Operator, RuleClause, SegmentRule

    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    segment = await engine.get_segment(key)
    if segment is None:
        return HTMLResponse("<p class='text-red-600 text-sm p-3'>Segment not found.</p>", 404)

    form = await request.form()
    description = str(form.get("description", "")).strip()
    attribute = str(form.get("attribute", "")).strip()
    operator_str = str(form.get("operator", "is")).strip()
    values_raw = str(form.get("values", "")).strip()
    negate = bool(form.get("negate"))

    # For segment operators the attribute is implicitly "key"
    is_seg_op = operator_str in ("in_segment", "not_in_segment")
    if is_seg_op:
        attribute = "key"

    if not attribute or not values_raw:
        return HTMLResponse(
            "<p class='text-red-600 text-sm p-3'>Attribute and values are required.</p>",
            status_code=400,
        )

    try:
        op = Operator(operator_str)
    except ValueError:
        return HTMLResponse(
            f"<p class='text-red-600 text-sm p-3'>Unknown operator: {operator_str}</p>",
            status_code=400,
        )

    values: list[str] = [v.strip() for v in values_raw.split(",") if v.strip()]
    clause = RuleClause(attribute=attribute, operator=op, values=values, negate=negate)
    rule = SegmentRule(id=str(_uuid.uuid4()), description=description, clauses=[clause])

    rules = list(segment.rules) + [rule]
    segment = segment.model_copy(update={"rules": rules})
    await engine.save_segment(segment, actor=_actor(request), platform=_platform(request))
    return tpl.TemplateResponse(
        request,
        "partials/segment_rules_section.html",
        {"prefix": prefix, "segment": segment, "key": key},
    )


async def segment_rule_delete(request: Request) -> Response:
    """DELETE /segments/{key}/rules/{rule_id} — remove a targeting rule."""
    tpl = _templates(request)
    engine = _engine(request)
    prefix = _prefix(request)
    key = request.path_params["key"]
    rule_id = request.path_params["rule_id"]
    segment = await engine.get_segment(key)
    if segment is None:
        return HTMLResponse("<p class='text-red-600 text-sm p-3'>Segment not found.</p>", 404)

    rules = [r for r in segment.rules if r.id != rule_id]
    segment = segment.model_copy(update={"rules": rules})
    await engine.save_segment(segment, actor=_actor(request), platform=_platform(request))
    return tpl.TemplateResponse(
        request,
        "partials/segment_rules_section.html",
        {"prefix": prefix, "segment": segment, "key": key},
    )
