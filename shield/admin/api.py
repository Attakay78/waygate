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

import asyncio
import base64
import logging
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from shield.core.engine import ShieldEngine
from shield.core.exceptions import (
    AmbiguousRouteError,
    RouteNotFoundException,
    RouteProtectedException,
)
from shield.core.models import AuditEntry, MaintenanceWindow, RouteState

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
    platform = body.get("platform", "cli") if isinstance(body, dict) else "cli"
    if platform not in ("cli", "sdk"):
        platform = "cli"

    if not username or not password:
        return _err("username and password are required")

    if not auth_backend.authenticate_user(username, password):
        return _err("Invalid credentials", 401)

    token, expires_at = tm.create(username, platform=platform)
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
    """GET /api/routes — list all registered route states.

    Optional query param ``?service=<name>`` filters to a single service.
    """
    states = await _engine(request).list_states()
    service = request.query_params.get("service")
    if service:
        states = [s for s in states if s.service == service]
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
    """GET /api/audit — return audit log entries (newest first).

    Optional query params:
    - ``?route=<path>`` — filter by exact route path
    - ``?service=<name>`` — filter to a single service (SDK mode)
    """
    route = request.query_params.get("route")
    service = request.query_params.get("service")
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    entries = await _engine(request).get_audit_log(path=route, limit=limit)
    if service:
        entries = [e for e in entries if e.service == service]
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


async def service_maintenance_get(request: Request) -> JSONResponse:
    """GET /api/services/{service}/maintenance — current per-service maintenance config."""
    service = request.path_params["service"]
    cfg = await _engine(request).get_service_maintenance(service)
    return JSONResponse(cfg.model_dump(mode="json"))


async def service_maintenance_enable(request: Request) -> JSONResponse:
    """POST /api/services/{service}/maintenance/enable — enable per-service maintenance."""
    service = request.path_params["service"]
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "") if isinstance(body, dict) else ""
    exempt = body.get("exempt_paths", []) if isinstance(body, dict) else []
    include_fa = body.get("include_force_active", False) if isinstance(body, dict) else False
    cfg = await _engine(request).enable_service_maintenance(
        service=service,
        reason=reason,
        exempt_paths=exempt,
        include_force_active=include_fa,
        actor=actor,
        platform=_platform(request),
    )
    return JSONResponse(cfg.model_dump(mode="json"))


async def service_maintenance_disable(request: Request) -> JSONResponse:
    """POST /api/services/{service}/maintenance/disable — disable per-service maintenance."""
    service = request.path_params["service"]
    actor = _actor(request)
    cfg = await _engine(request).disable_service_maintenance(
        service=service, actor=actor, platform=_platform(request)
    )
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
    except RouteNotFoundException as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
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


# ---------------------------------------------------------------------------
# Per-service rate limit endpoints
# ---------------------------------------------------------------------------


async def get_service_rate_limit(request: Request) -> JSONResponse:
    """GET /api/services/{service}/rate-limit — current per-service rate limit policy."""
    service = request.path_params["service"]
    policy = await _engine(request).get_service_rate_limit(service)
    if policy is None:
        return JSONResponse({"enabled": False, "policy": None})
    return JSONResponse({"enabled": policy.enabled, "policy": policy.model_dump(mode="json")})


async def set_service_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/services/{service}/rate-limit — set or update per-service rate limit."""
    service = request.path_params["service"]
    engine = _engine(request)
    actor = _actor(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    limit = body.get("limit", "")
    if not limit:
        return JSONResponse({"error": "limit is required"}, status_code=400)
    exempt_routes = body.get("exempt_routes", [])
    if not isinstance(exempt_routes, list):
        return JSONResponse({"error": "exempt_routes must be a list"}, status_code=400)

    try:
        policy = await engine.set_service_rate_limit(
            service,
            limit=limit,
            algorithm=body.get("algorithm"),
            key_strategy=body.get("key_strategy"),
            on_missing_key=body.get("on_missing_key"),
            burst=int(body.get("burst", 0)),
            exempt_routes=exempt_routes,
            actor=actor,
            platform=_platform(request),
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(policy.model_dump(mode="json"), status_code=201)


async def delete_service_rate_limit_api(request: Request) -> JSONResponse:
    """DELETE /api/services/{service}/rate-limit — remove per-service rate limit policy."""
    service = request.path_params["service"]
    await _engine(request).delete_service_rate_limit(
        service, actor=_actor(request), platform=_platform(request)
    )
    return JSONResponse({"ok": True})


async def reset_service_rate_limit_api(request: Request) -> JSONResponse:
    """DELETE /api/services/{service}/rate-limit/reset — reset per-service counters."""
    service = request.path_params["service"]
    await _engine(request).reset_service_rate_limit(
        service, actor=_actor(request), platform=_platform(request)
    )
    return JSONResponse({"ok": True})


async def enable_service_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/services/{service}/rate-limit/enable — resume paused service rate limit."""
    service = request.path_params["service"]
    await _engine(request).enable_service_rate_limit(
        service, actor=_actor(request), platform=_platform(request)
    )
    return JSONResponse({"ok": True})


async def disable_service_rate_limit_api(request: Request) -> JSONResponse:
    """POST /api/services/{service}/rate-limit/disable — pause service rate limit."""
    service = request.path_params["service"]
    await _engine(request).disable_service_rate_limit(
        service, actor=_actor(request), platform=_platform(request)
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# SDK endpoints — used by ShieldServerBackend / ShieldSDK clients
# ---------------------------------------------------------------------------


async def sdk_events(request: Request) -> StreamingResponse:
    """GET /api/sdk/events — SSE stream of typed route state and RL policy changes.

    SDK clients (``ShieldServerBackend``) connect here to keep their
    local cache current without polling.  Each event is a typed JSON
    envelope:

    * Route state change::

        data: {"type": "state", "payload": {...RouteState...}}

    * Rate limit policy change::

        data: {"type": "rl_policy", "action": "set", "key": "GET:/api/pay", "policy": {...}}
        data: {"type": "rl_policy", "action": "delete", "key": "GET:/api/pay"}

    When a backend does not support ``subscribe()`` (e.g. FileBackend)
    the endpoint falls back to 15-second keepalive pings so clients
    maintain their connection and rely on the full re-sync performed
    after each reconnect.
    """
    import json as _json

    engine = _engine(request)
    queue: asyncio.Queue[str] = asyncio.Queue()
    tasks: list[asyncio.Task[None]] = []

    async def _feed_states() -> None:
        try:
            async for state in engine.backend.subscribe():
                envelope = _json.dumps({"type": "state", "payload": state.model_dump(mode="json")})
                await queue.put(f"data: {envelope}\n\n")
        except NotImplementedError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shield: SDK SSE state subscription error")

    async def _feed_rl_policies() -> None:
        try:
            async for event in engine.backend.subscribe_rate_limit_policy():
                envelope = _json.dumps({"type": "rl_policy", **event})
                await queue.put(f"data: {envelope}\n\n")
        except NotImplementedError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shield: SDK SSE RL policy subscription error")

    async def _feed_flags() -> None:
        try:
            async for event in engine.backend.subscribe_flag_changes():  # type: ignore[attr-defined]
                envelope = _json.dumps(event)
                await queue.put(f"data: {envelope}\n\n")
        except (NotImplementedError, AttributeError):
            # Backend doesn't support flag pub/sub — silently skip.
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shield: SDK SSE flag subscription error")

    async def _generate() -> object:
        tasks.append(asyncio.create_task(_feed_states()))
        tasks.append(asyncio.create_task(_feed_rl_policies()))
        tasks.append(asyncio.create_task(_feed_flags()))
        try:
            while True:
                # Check for client disconnect before blocking on the queue.
                # is_disconnected() polls receive() with a 1 ms timeout so it
                # never blocks the loop for more than a millisecond.
                if await request.is_disconnected():
                    break
                try:
                    # Block until an event arrives or 15 s elapses.
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield msg
                except TimeoutError:
                    # No event in 15 s — send a keepalive comment to hold the connection.
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            for t in tasks:
                t.cancel()
            # Await the feeder tasks so their finally blocks (which deregister
            # subscriber queues) run before this handler returns.  Errors are
            # suppressed — we only care that cleanup completes.
            await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(
        _generate(),  # type: ignore[arg-type]
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def sdk_register(request: Request) -> JSONResponse:
    """POST /api/sdk/register — batch-register routes from an SDK client.

    Applies server-wins semantics: routes that already exist in the
    backend are left untouched and their current state is returned.
    New routes are created with the initial state supplied by the SDK.

    The SDK sends states with ``path = "{app_id}:{original_path}"`` and
    ``service = app_id`` already set.  This endpoint trusts those values
    directly — no further rewriting is done here.

    Request body::

        {
            "app_id": "payments-service",
            "states": [ ...RouteState dicts with service-prefixed paths... ]
        }

    Response::

        {"states": [ ...current RouteState dicts... ]}
    """
    engine = _engine(request)
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    app_id = body.get("app_id", "unknown") if isinstance(body, dict) else "unknown"
    states_data = body.get("states", []) if isinstance(body, dict) else []
    if not isinstance(states_data, list):
        return _err("states must be a list")

    results: list[dict[str, Any]] = []
    for state_dict in states_data:
        try:
            incoming = RouteState.model_validate(state_dict)
        except Exception:
            continue

        # Ensure service field is always populated from app_id for legacy clients
        # that do not set it themselves.
        if not incoming.service:
            incoming = incoming.model_copy(update={"service": app_id})

        # Server-wins: if this namespaced key already exists, keep server state.
        try:
            existing = await engine.backend.get_state(incoming.path)
            results.append(existing.model_dump(mode="json"))
        except KeyError:
            await engine.backend.set_state(incoming.path, incoming)
            results.append(incoming.model_dump(mode="json"))

    logger.debug("shield: SDK registered %d route(s) from app_id=%s", len(results), app_id)
    return JSONResponse({"states": results})


async def list_services(request: Request) -> JSONResponse:
    """GET /api/services — return the distinct service names across all routes.

    Used by the dashboard dropdown and CLI to discover which services have
    registered routes with this Shield Server.  Routes without a service
    (embedded-mode routes) are not included.
    """
    states = await _engine(request).list_states()
    services = sorted({s.service for s in states if s.service})
    return JSONResponse(services)


async def sdk_audit(request: Request) -> JSONResponse:
    """POST /api/sdk/audit — receive an audit entry forwarded by an SDK client.

    SDK clients forward audit entries here so the Shield Server maintains
    a unified audit log across all connected services.
    """
    engine = _engine(request)
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    try:
        entry = AuditEntry.model_validate(body)
    except Exception as exc:
        return _err(f"Invalid audit entry: {exc}")

    await engine.backend.write_audit(entry)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Feature flag endpoints
# ---------------------------------------------------------------------------
#
# These endpoints are only mounted when ShieldAdmin(enable_flags=True).
# They require the [flags] optional extra to be installed — callers get a
# clear 501 error if the extra is missing.
# ---------------------------------------------------------------------------


def _flags_not_configured() -> JSONResponse:
    return JSONResponse(
        {
            "error": (
                "Feature flags are not enabled. "
                "Call engine.use_openfeature() and set enable_flags=True on ShieldAdmin."
            )
        },
        status_code=501,
    )


def _flags_not_installed() -> JSONResponse:
    return JSONResponse(
        {
            "error": (
                "Feature flags require the [flags] extra. "
                "Install with: pip install api-shield[flags]"
            )
        },
        status_code=501,
    )


def _flag_models_available() -> bool:
    """Return True if the openfeature extra is installed."""
    try:
        import openfeature  # noqa: F401

        return True
    except ImportError:
        return False


async def list_flags(request: Request) -> JSONResponse:
    """GET /api/flags — list all feature flags."""
    if not _flag_models_available():
        return _flags_not_installed()
    flags = await _engine(request).list_flags()
    return JSONResponse([f.model_dump(mode="json") for f in flags])


async def get_flag(request: Request) -> JSONResponse:
    """GET /api/flags/{key} — get a single feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    flag = await _engine(request).get_flag(key)
    if flag is None:
        return _err(f"Flag '{key}' not found", 404)
    return JSONResponse(flag.model_dump(mode="json"))


async def create_flag(request: Request) -> JSONResponse:
    """POST /api/flags — create a new feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    try:
        from shield.core.feature_flags.models import FeatureFlag

        flag = FeatureFlag.model_validate(body)
    except Exception as exc:
        return _err(f"Invalid flag definition: {exc}")

    # Conflict check
    existing = await _engine(request).get_flag(flag.key)
    if existing is not None:
        return _err(f"Flag '{flag.key}' already exists. Use PUT to update.", 409)

    await _engine(request).save_flag(flag, actor=_actor(request), platform=_platform(request))
    return JSONResponse(flag.model_dump(mode="json"), status_code=201)


async def update_flag(request: Request) -> JSONResponse:
    """PUT /api/flags/{key} — replace a feature flag (full update)."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    # Key in URL must match key in body if provided.
    if isinstance(body, dict) and body.get("key", key) != key:
        return _err("Flag key in URL and body must match")

    if isinstance(body, dict):
        body["key"] = key

    try:
        from shield.core.feature_flags.models import FeatureFlag

        flag = FeatureFlag.model_validate(body)
    except Exception as exc:
        return _err(f"Invalid flag definition: {exc}")

    await _engine(request).save_flag(flag, actor=_actor(request), platform=_platform(request))
    return JSONResponse(flag.model_dump(mode="json"))


async def patch_flag(request: Request) -> JSONResponse:
    """PATCH /api/flags/{key} — partial update of a feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    flag = await _engine(request).get_flag(key)
    if flag is None:
        return _err(f"Flag '{key}' not found", 404)
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")
    if not isinstance(body, dict):
        return _err("Body must be a JSON object")

    # Never allow patching immutable fields
    for immutable in ("key", "type"):
        body.pop(immutable, None)

    try:
        from shield.core.feature_flags.models import FeatureFlag

        # Build updated flag by merging patch onto existing
        current = flag.model_dump(mode="python")
        current.update(body)
        updated = FeatureFlag.model_validate(current)
    except Exception as exc:
        return _err(f"Invalid patch: {exc}")

    # Cross-field validation: off_variation and string fallthrough must name
    # an existing variation (the model doesn't enforce this itself).
    variation_names = {v.name for v in updated.variations}
    if updated.off_variation not in variation_names:
        return _err(f"off_variation '{updated.off_variation}' does not match any variation name")
    if isinstance(updated.fallthrough, str) and updated.fallthrough not in variation_names:
        return _err(f"fallthrough '{updated.fallthrough}' does not match any variation name")

    await _engine(request).save_flag(updated, actor=_actor(request), platform=_platform(request))
    return JSONResponse(updated.model_dump(mode="json"))


async def enable_flag(request: Request) -> JSONResponse:
    """POST /api/flags/{key}/enable — enable a feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    flag = await _engine(request).get_flag(key)
    if flag is None:
        return _err(f"Flag '{key}' not found", 404)
    flag = flag.model_copy(update={"enabled": True})
    await _engine(request).save_flag(
        flag, actor=_actor(request), platform=_platform(request), action="flag_enabled"
    )
    return JSONResponse(flag.model_dump(mode="json"))


async def disable_flag(request: Request) -> JSONResponse:
    """POST /api/flags/{key}/disable — disable a feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    flag = await _engine(request).get_flag(key)
    if flag is None:
        return _err(f"Flag '{key}' not found", 404)
    flag = flag.model_copy(update={"enabled": False})
    await _engine(request).save_flag(
        flag, actor=_actor(request), platform=_platform(request), action="flag_disabled"
    )
    return JSONResponse(flag.model_dump(mode="json"))


async def delete_flag(request: Request) -> JSONResponse:
    """DELETE /api/flags/{key} — delete a feature flag."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    existing = await _engine(request).get_flag(key)
    if existing is None:
        return _err(f"Flag '{key}' not found", 404)
    await _engine(request).delete_flag(key, actor=_actor(request), platform=_platform(request))
    return JSONResponse({"ok": True, "deleted": key})


async def evaluate_flag(request: Request) -> JSONResponse:
    """POST /api/flags/{key}/evaluate — evaluate a flag for a given context.

    Body: ``{"default": <value>, "context": {"key": "user_1", "attributes": {...}}}``

    Returns the resolved value, variation, reason, and any metadata.
    Useful for debugging targeting rules from the dashboard or CLI.
    """
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]

    flag = await _engine(request).get_flag(key)
    if flag is None:
        return _err(f"Flag '{key}' not found", 404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    ctx_data = body.get("context", {}) if isinstance(body, dict) else {}

    try:
        from shield.core.feature_flags.evaluator import FlagEvaluator
        from shield.core.feature_flags.models import EvaluationContext

        ctx = EvaluationContext.model_validate({"key": "anonymous", **ctx_data})
        engine = _engine(request)
        # Gather all flags and segments from the engine for prerequisite resolution.
        all_flags_list = await engine.list_flags()
        all_flags = {f.key: f for f in all_flags_list}
        segments_list = await engine.list_segments()
        segments = {s.key: s for s in segments_list}

        evaluator = FlagEvaluator(segments=segments)
        result = evaluator.evaluate(flag, ctx, all_flags)
    except Exception as exc:
        return _err(f"Evaluation error: {exc}", 500)

    return JSONResponse(
        {
            "flag_key": key,
            "value": result.value,
            "variation": result.variation,
            "reason": result.reason.value,
            "rule_id": result.rule_id,
            "prerequisite_key": result.prerequisite_key,
            "error_message": result.error_message,
        }
    )


# ---------------------------------------------------------------------------
# Segment endpoints
# ---------------------------------------------------------------------------


async def list_segments(request: Request) -> JSONResponse:
    """GET /api/segments — list all segments."""
    if not _flag_models_available():
        return _flags_not_installed()
    segments = await _engine(request).list_segments()
    return JSONResponse([s.model_dump(mode="json") for s in segments])


async def get_segment(request: Request) -> JSONResponse:
    """GET /api/segments/{key} — get a single segment."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    segment = await _engine(request).get_segment(key)
    if segment is None:
        return _err(f"Segment '{key}' not found", 404)
    return JSONResponse(segment.model_dump(mode="json"))


async def create_segment(request: Request) -> JSONResponse:
    """POST /api/segments — create a new segment."""
    if not _flag_models_available():
        return _flags_not_installed()
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    try:
        from shield.core.feature_flags.models import Segment

        segment = Segment.model_validate(body)
    except Exception as exc:
        return _err(f"Invalid segment definition: {exc}")

    existing = await _engine(request).get_segment(segment.key)
    if existing is not None:
        return _err(f"Segment '{segment.key}' already exists. Use PUT to update.", 409)

    await _engine(request).save_segment(segment, actor=_actor(request), platform=_platform(request))
    return JSONResponse(segment.model_dump(mode="json"), status_code=201)


async def update_segment(request: Request) -> JSONResponse:
    """PUT /api/segments/{key} — replace a segment (full update)."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON body")

    if isinstance(body, dict) and body.get("key", key) != key:
        return _err("Segment key in URL and body must match")

    if isinstance(body, dict):
        body["key"] = key

    try:
        from shield.core.feature_flags.models import Segment

        segment = Segment.model_validate(body)
    except Exception as exc:
        return _err(f"Invalid segment definition: {exc}")

    await _engine(request).save_segment(segment, actor=_actor(request), platform=_platform(request))
    return JSONResponse(segment.model_dump(mode="json"))


async def delete_segment(request: Request) -> JSONResponse:
    """DELETE /api/segments/{key} — delete a segment."""
    if not _flag_models_available():
        return _flags_not_installed()
    key = request.path_params["key"]
    existing = await _engine(request).get_segment(key)
    if existing is None:
        return _err(f"Segment '{key}' not found", 404)
    await _engine(request).delete_segment(key, actor=_actor(request), platform=_platform(request))
    return JSONResponse({"ok": True, "deleted": key})
