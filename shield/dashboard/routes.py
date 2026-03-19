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


async def index(request: Request) -> Response:
    """Render the main routes page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    states = await engine.list_states()
    global_config = await engine.get_global_maintenance()
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
            "rate_limit_policies": rl_by_path,
            "prefix": prefix,
            "active_tab": "routes",
            "version": request.app.state.version,
            "path_slug": path_slug,
            "shield_actor": _actor(request),
        },
    )


async def routes_partial(request: Request) -> Response:
    """Return only the routes table rows (HTMX polling fallback)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    states = await engine.list_states()
    return tpl.TemplateResponse(
        request,
        "partials/routes_table.html",
        {
            "states": states,
            "prefix": prefix,
            "path_slug": path_slug,
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
    entries = await engine.get_audit_log(limit=1000)
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
        },
    )


async def audit_rows(request: Request) -> Response:
    """Return only the audit log rows partial (for HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)

    entries = await engine.get_audit_log(limit=50)
    return tpl.TemplateResponse(
        request,
        "partials/audit_rows.html",
        {"entries": entries},
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
    policies = list(engine._rate_limit_policies.values())
    paged = _paginate(policies, page)
    return tpl.TemplateResponse(
        request,
        "rate_limits.html",
        {
            "policies": paged["items"],
            "pagination": paged,
            "prefix": prefix,
            "active_tab": "rate_limits",
            "version": request.app.state.version,
            "shield_actor": _actor(request),
        },
    )


async def rl_hits_page(request: Request) -> Response:
    """Render the blocked requests page (full page)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    hits = await engine.get_rate_limit_hits(limit=10_000)
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
        },
    )


async def rate_limits_rows_partial(request: Request) -> Response:
    """Return only the rate limit policies table rows (HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)
    prefix = _prefix(request)

    page = int(request.query_params.get("page", 1))
    policies = list(engine._rate_limit_policies.values())
    paged = _paginate(policies, page)
    return tpl.TemplateResponse(
        request,
        "partials/rate_limit_rows.html",
        {"policies": paged["items"], "prefix": prefix},
    )


def _render_rl_row(tpl: Jinja2Templates, policy: Any, prefix: str) -> str:
    """Render the rate_limit_rows.html partial for a single policy."""
    return tpl.env.get_template("partials/rate_limit_rows.html").render(
        policies=[policy],
        prefix=prefix,
    )


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
    await engine.set_rate_limit_policy(
        route_path,
        method,
        limit,
        algorithm=algorithm,
        key_strategy=key_strategy,
        actor=_actor(request),
        platform=_platform(request),
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


async def rate_limits_hits_partial(request: Request) -> Response:
    """Return only the recent blocked requests table rows (HTMX auto-refresh)."""
    engine = _engine(request)
    tpl = _templates(request)

    hits = await engine.get_rate_limit_hits(limit=50)
    return tpl.TemplateResponse(
        request,
        "partials/rate_limit_hits.html",
        {"hits": hits},
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

    async def _generate() -> object:
        try:
            async for state in engine.backend.subscribe():
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
