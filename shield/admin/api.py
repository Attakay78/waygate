"""REST API route handlers for ShieldAdmin.

All handlers live under ``/api/`` within the mounted admin app and return
JSON responses.  The CLI uses these endpoints as its back-end.

Auth
----
Requests must carry a valid ``X-Shield-Token: <token>`` header.
When auth is not configured on the server every request is accepted and
the actor defaults to ``"anonymous"``.

Actor / Platform
----------------
Every mutating handler reads ``request.state.shield_actor`` and
``request.state.shield_platform`` (injected by the auth middleware) so
that audit log entries record who made the change and from which surface.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse

from shield.core.engine import ShieldEngine
from shield.core.exceptions import (
    AmbiguousRouteError,
    RouteNotFoundException,
    RouteProtectedException,
)
from shield.core.models import MaintenanceWindow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine(request: Request) -> ShieldEngine:
    """Return the ShieldEngine from app state."""
    return request.app.state.engine  # type: ignore[no-any-return]


def _actor(request: Request) -> str:
    """Return the authenticated actor name from request state."""
    return getattr(request.state, "shield_actor", "anonymous")


def _platform(request: Request) -> str:
    """Return the authenticated platform from request state."""
    return getattr(request.state, "shield_platform", "cli")


def _decode_path(encoded: str) -> str:
    """Decode a base64url-encoded route path key from a URL segment."""
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    return base64.urlsafe_b64decode(encoded).decode()


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _err_ambiguous(exc: AmbiguousRouteError) -> JSONResponse:
    return JSONResponse(
        {"error": str(exc), "ambiguous_matches": exc.matches},
        status_code=409,
    )


def _extract_token(request: Request) -> str | None:
    value = request.headers.get("X-Shield-Token", "").strip()
    return value or None


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


async def auth_login(request: Request) -> JSONResponse:
    """POST /api/auth/login — exchange credentials for a token."""
    tm = request.app.state.token_manager
    auth_backend = request.app.state.auth_backend

    if auth_backend is None:
        return _err("Auth not configured on this server", 501)

    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    username = body.get("username", "") if isinstance(body, dict) else ""
    password = body.get("password", "") if isinstance(body, dict) else ""

    if not username or not password:
        return _err("username and password are required")

    if not auth_backend.authenticate_user(username, password):
        return _err("Invalid credentials", 401)

    token, expires_at = tm.create(username, platform="cli")
    return JSONResponse(
        {
            "token": token,
            "username": username,
            "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat(),
        }
    )


async def auth_logout(request: Request) -> JSONResponse:
    """POST /api/auth/logout — revoke the current bearer token."""
    token = _extract_token(request)
    if token:
        request.app.state.token_manager.revoke(token)
    return JSONResponse({"ok": True})


async def auth_me(request: Request) -> JSONResponse:
    """GET /api/auth/me — info about the authenticated user."""
    return JSONResponse({"username": _actor(request), "platform": _platform(request)})


# ---------------------------------------------------------------------------
# Route state endpoints
# ---------------------------------------------------------------------------


async def list_routes(request: Request) -> JSONResponse:
    """GET /api/routes — list all registered route states."""
    states = await _engine(request).list_states()
    return JSONResponse([s.model_dump(mode="json") for s in states])


async def get_route(request: Request) -> JSONResponse:
    """GET /api/routes/{path_key} — get state for one route."""
    path = _decode_path(request.path_params["path_key"])
    state = await _engine(request).get_state(path)
    return JSONResponse(state.model_dump(mode="json"))


async def enable_route(request: Request) -> JSONResponse:
    """POST /api/routes/{path_key}/enable — enable a route."""
    path = _decode_path(request.path_params["path_key"])
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    try:
        state = await _engine(request).enable(
            path, actor=actor, reason=reason, platform=_platform(request)
        )
    except RouteNotFoundException as exc:
        return _err(str(exc), 404)
    except AmbiguousRouteError as exc:
        return _err_ambiguous(exc)
    except RouteProtectedException as exc:
        return _err(str(exc), 409)
    return JSONResponse(state.model_dump(mode="json"))


async def disable_route(request: Request) -> JSONResponse:
    """POST /api/routes/{path_key}/disable — disable a route."""
    path = _decode_path(request.path_params["path_key"])
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    try:
        state = await _engine(request).disable(
            path, actor=actor, reason=reason, platform=_platform(request)
        )
    except RouteNotFoundException as exc:
        return _err(str(exc), 404)
    except AmbiguousRouteError as exc:
        return _err_ambiguous(exc)
    except RouteProtectedException as exc:
        return _err(str(exc), 409)
    return JSONResponse(state.model_dump(mode="json"))


async def maintenance_route(request: Request) -> JSONResponse:
    """POST /api/routes/{path_key}/maintenance — put a route in maintenance mode."""
    path = _decode_path(request.path_params["path_key"])
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    window = None
    if isinstance(body, dict):
        s, e = body.get("start"), body.get("end")
        if s and e:
            try:
                sd = datetime.fromisoformat(s)
                ed = datetime.fromisoformat(e)
                sd = sd if sd.tzinfo else sd.replace(tzinfo=UTC)
                ed = ed if ed.tzinfo else ed.replace(tzinfo=UTC)
                window = MaintenanceWindow(start=sd, end=ed, reason=reason)
            except ValueError:
                return _err("Invalid datetime for start/end")
    try:
        state = await _engine(request).set_maintenance(
            path, reason=reason, window=window, actor=actor, platform=_platform(request)
        )
    except RouteNotFoundException as exc:
        return _err(str(exc), 404)
    except AmbiguousRouteError as exc:
        return _err_ambiguous(exc)
    except RouteProtectedException as exc:
        return _err(str(exc), 409)
    return JSONResponse(state.model_dump(mode="json"))


async def env_route(request: Request) -> JSONResponse:
    """POST /api/routes/{path_key}/env — restrict route to specific environments."""
    path = _decode_path(request.path_params["path_key"])
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")
    envs = body.get("envs", []) if isinstance(body, dict) else []
    if not isinstance(envs, list):
        return _err("envs must be a list of strings")
    try:
        state = await _engine(request).set_env_only(
            path, envs=envs, actor=actor, platform=_platform(request)
        )
    except RouteNotFoundException as exc:
        return _err(str(exc), 404)
    except AmbiguousRouteError as exc:
        return _err_ambiguous(exc)
    except RouteProtectedException as exc:
        return _err(str(exc), 409)
    return JSONResponse(state.model_dump(mode="json"))


async def schedule_route(request: Request) -> JSONResponse:
    """POST /api/routes/{path_key}/schedule — schedule a maintenance window."""
    path = _decode_path(request.path_params["path_key"])
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")
    if not isinstance(body, dict):
        return _err("JSON body must be an object")
    s, e = body.get("start"), body.get("end")
    if not s or not e:
        return _err("start and end are required")
    reason = body.get("reason", "")
    try:
        sd = datetime.fromisoformat(s)
        ed = datetime.fromisoformat(e)
        sd = sd if sd.tzinfo else sd.replace(tzinfo=UTC)
        ed = ed if ed.tzinfo else ed.replace(tzinfo=UTC)
    except ValueError:
        return _err("Invalid datetime for start/end")
    window = MaintenanceWindow(start=sd, end=ed, reason=reason)
    try:
        await _engine(request).schedule_maintenance(
            path, window, actor=actor, platform=_platform(request)
        )
    except RouteNotFoundException as exc:
        return _err(str(exc), 404)
    except AmbiguousRouteError as exc:
        return _err_ambiguous(exc)
    except RouteProtectedException as exc:
        return _err(str(exc), 409)
    state = await _engine(request).get_state(path)
    return JSONResponse(state.model_dump(mode="json"))


async def cancel_schedule_route(request: Request) -> JSONResponse:
    """DELETE /api/routes/{path_key}/schedule — cancel a pending maintenance window."""
    path = _decode_path(request.path_params["path_key"])
    await _engine(request).scheduler.cancel(path)
    state = await _engine(request).get_state(path)
    return JSONResponse(state.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def list_audit(request: Request) -> JSONResponse:
    """GET /api/audit — return audit log entries (newest first)."""
    route = request.query_params.get("route")
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    entries = await _engine(request).get_audit_log(path=route, limit=limit)
    return JSONResponse([e.model_dump(mode="json") for e in entries])


# ---------------------------------------------------------------------------
# Global maintenance
# ---------------------------------------------------------------------------


async def get_global(request: Request) -> JSONResponse:
    """GET /api/global — current global maintenance configuration."""
    cfg = await _engine(request).get_global_maintenance()
    return JSONResponse(cfg.model_dump(mode="json"))


async def global_enable_api(request: Request) -> JSONResponse:
    """POST /api/global/enable — enable global maintenance mode."""
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    exempt = body.get("exempt_paths", []) if isinstance(body, dict) else []
    include_fa = body.get("include_force_active", False) if isinstance(body, dict) else False
    await _engine(request).enable_global_maintenance(
        reason=reason,
        exempt_paths=exempt,
        include_force_active=include_fa,
        actor=actor,
        platform=_platform(request),
    )
    cfg = await _engine(request).get_global_maintenance()
    return JSONResponse(cfg.model_dump(mode="json"))


async def global_disable_api(request: Request) -> JSONResponse:
    """POST /api/global/disable — disable global maintenance mode."""
    actor = _actor(request)
    await _engine(request).disable_global_maintenance(actor=actor, platform=_platform(request))
    cfg = await _engine(request).get_global_maintenance()
    return JSONResponse(cfg.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


async def list_rate_limits(request: Request) -> JSONResponse:
    """GET /api/rate-limits — list all registered rate limit policies."""
    engine = _engine(request)
    policies = [p.model_dump(mode="json") for p in engine._rate_limit_policies.values()]
    return JSONResponse(policies)


async def get_rate_limit_hits(request: Request) -> JSONResponse:
    """GET /api/rate-limits/hits — return recent rate limit hits."""
    engine = _engine(request)
    route = request.query_params.get("route")
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    hits = await engine.get_rate_limit_hits(path=route, limit=limit)
    return JSONResponse([h.model_dump(mode="json") for h in hits])


async def reset_rate_limit(request: Request) -> JSONResponse:
    """DELETE /api/rate-limits/{path_key}/reset — reset counters for a route."""
    engine = _engine(request)
    path = _decode_path(request.path_params["path_key"])
    method = request.query_params.get("method")
    await engine.reset_rate_limit(path=path, method=method or None)
    return JSONResponse({"ok": True, "path": path})


async def set_rate_limit_policy_api(request: Request) -> JSONResponse:
    """POST /api/rate-limits — create or update a rate limit policy."""
    engine = _engine(request)
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    path = body.get("path")
    method = body.get("method", "GET")
    limit = body.get("limit")
    if not path or not limit:
        return JSONResponse({"error": "path and limit are required"}, status_code=400)

    try:
        policy = await engine.set_rate_limit_policy(
            path=path,
            method=method,
            limit=limit,
            algorithm=body.get("algorithm"),
            key_strategy=body.get("key_strategy"),
            burst=int(body.get("burst", 0)),
            actor=actor,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(policy.model_dump(mode="json"), status_code=201)


async def delete_rate_limit_policy_api(request: Request) -> JSONResponse:
    """DELETE /api/rate-limits/{path_key} — remove a rate limit policy.

    ``path_key`` is a base64url-encoded string in the form ``METHOD:path``
    (e.g. ``GET:/api/items``).  Use :func:`shield.cli.client._encode_path`
    to produce the correct encoding.
    """
    engine = _engine(request)
    actor = _actor(request)
    # base64url-decode the composite key ("METHOD:/path")
    raw_key = _decode_path(request.path_params["path_key"])
    if ":" not in raw_key:
        return JSONResponse({"error": "path_key must encode METHOD:path"}, status_code=400)
    method, path = raw_key.split(":", 1)
    await engine.delete_rate_limit_policy(path=path, method=method, actor=actor)
    return JSONResponse({"ok": True, "path": path, "method": method})


# ---------------------------------------------------------------------------
# Global rate limit
# ---------------------------------------------------------------------------


async def get_global_rate_limit(request: Request) -> JSONResponse:
    """GET /api/global-rate-limit — current global rate limit policy."""
    policy = await _engine(request).get_global_rate_limit()
    if policy is None:
        return JSONResponse({"enabled": False, "policy": None})
    return JSONResponse({"enabled": policy.enabled, "policy": policy.model_dump(mode="json")})


async def set_global_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/global-rate-limit — set or update the global rate limit policy."""
    engine = _engine(request)
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    limit = body.get("limit")
    if not limit:
        return JSONResponse({"error": "limit is required"}, status_code=400)

    exempt = body.get("exempt_routes", [])
    if not isinstance(exempt, list):
        return JSONResponse({"error": "exempt_routes must be a list"}, status_code=400)

    try:
        policy = await engine.set_global_rate_limit(
            limit=limit,
            algorithm=body.get("algorithm"),
            key_strategy=body.get("key_strategy"),
            on_missing_key=body.get("on_missing_key"),
            burst=int(body.get("burst", 0)),
            exempt_routes=exempt,
            actor=actor,
            platform=_platform(request),
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(policy.model_dump(mode="json"), status_code=201)


async def delete_global_rate_limit_api(request: Request) -> JSONResponse:
    """DELETE /api/global-rate-limit — remove the global rate limit policy."""
    engine = _engine(request)
    actor = _actor(request)
    await engine.delete_global_rate_limit(actor=actor, platform=_platform(request))
    return JSONResponse({"ok": True})


async def reset_global_rate_limit_api(request: Request) -> JSONResponse:
    """DELETE /api/global-rate-limit/reset — reset global rate limit counters."""
    engine = _engine(request)
    actor = _actor(request)
    await engine.reset_global_rate_limit(actor=actor, platform=_platform(request))
    return JSONResponse({"ok": True})


async def enable_global_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/global-rate-limit/enable — resume a paused global rate limit."""
    engine = _engine(request)
    actor = _actor(request)
    await engine.enable_global_rate_limit(actor=actor, platform=_platform(request))
    return JSONResponse({"ok": True})


async def disable_global_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/global-rate-limit/disable — pause the global rate limit."""
    engine = _engine(request)
    actor = _actor(request)
    await engine.disable_global_rate_limit(actor=actor, platform=_platform(request))
    return JSONResponse({"ok": True})
