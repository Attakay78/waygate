"""WaygateServerBackend — remote backend that delegates to a Waygate Server.

Route states are cached locally so ``get_state()`` never touches the
network.  An SSE connection keeps the cache fresh whenever the Waygate
Server broadcasts a change (enable, disable, maintenance, etc.).

Typical usage::

    from waygate.core.backends.server import WaygateServerBackend
    from waygate.core.engine import WaygateEngine

    backend = WaygateServerBackend(
        server_url="http://waygate-server:9000",
        app_id="payments-service",
        token="...",          # omit if server has no auth
    )
    engine = WaygateEngine(backend=backend)

Or use the higher-level :class:`~waygate.sdk.WaygateSDK` which wires
everything together automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from waygate.core.backends.base import _GLOBAL_KEY, WaygateBackend
from waygate.core.models import AuditEntry, GlobalMaintenanceConfig, RouteState

logger = logging.getLogger(__name__)


class WaygateServerBackend(WaygateBackend):
    """Backend that enforces rules from a remote Waygate Server.

    All enforcement happens against a local in-process cache — there is
    zero network overhead per request.  The cache is populated on startup
    via ``GET /api/routes`` and kept current by a persistent SSE
    connection to ``GET /api/sdk/events``.

    Parameters
    ----------
    server_url:
        Base URL of the Waygate Server, including any mount prefix.
        Example: ``http://waygate-server:9000`` or
        ``http://myapp.com/waygate``.
    app_id:
        Unique identifier for this service.  Shown in the Waygate Server
        dashboard to group routes by application.
    token:
        Pre-issued bearer token for Waygate Server auth.  Takes priority
        over ``username``/``password`` if both are provided.  ``None``
        if the server has no auth configured.
    username:
        Waygate Server username.  When provided alongside ``password``
        (and no ``token``), the SDK calls ``POST /api/auth/login`` with
        ``platform="sdk"`` on startup and caches the returned token for
        the lifetime of the process — no manual token management required.
    password:
        Waygate Server password.  Used with ``username`` for auto-login.
    reconnect_delay:
        Seconds to wait before reconnecting a dropped SSE stream.
        Defaults to 5 seconds.
    """

    def __init__(
        self,
        server_url: str,
        app_id: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        reconnect_delay: float = 5.0,
    ) -> None:
        self._base_url = server_url.rstrip("/")
        self._app_id = app_id
        self._token = token
        self._username = username
        self._password = password
        self._reconnect_delay = reconnect_delay

        # In-process cache — get_state() reads only from here.
        self._cache: dict[str, RouteState] = {}

        # Routes registered locally during startup before the HTTP client
        # exists, or before _startup_done is True.  Keyed by server-side
        # path so duplicates (e.g. from WaygateRouter + SDK scan) are collapsed.
        # Flushed to the server as one batch once _flush_pending() completes.
        self._pending: dict[str, RouteState] = {}

        # False until _flush_pending() completes for the first time.  While
        # False, set_state() always queues to _pending instead of firing
        # individual _push_state() tasks so that ALL startup registrations —
        # from WaygateRouter, from SDK route scan, from any ordering of startup
        # hooks — travel in exactly ONE HTTP round-trip at the end.  Once True,
        # real-time pushes are enabled for runtime state mutations (enable,
        # disable, maintenance, etc.).
        self._startup_done: bool = False

        # Queues signalled whenever the global-maintenance config changes
        # (all-services global key OR the service-specific key).  Used by
        # subscribe_global_config() so the engine can invalidate its cache.
        self._global_config_changed: list[asyncio.Queue[None]] = []

        # Local rate limit policy cache — keyed "METHOD:local_path" → policy dict.
        self._rl_policy_cache: dict[str, dict[str, Any]] = {}
        self._rl_policy_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

        # Queues for state-change subscribers (fed by _listen_sse on "state" events).
        # Consumed by subscribe() so _run_route_state_listener bumps _schema_version,
        # which invalidates the apply_waygate_to_openapi cache on every SSE update.
        self._state_subscribers: list[asyncio.Queue[RouteState]] = []

        # Local feature flag / segment cache (populated by SSE flag events).
        self._flag_cache: dict[str, Any] = {}  # key → FeatureFlag raw dict
        self._segment_cache: dict[str, Any] = {}  # key → Segment raw dict
        self._flag_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"X-Waygate-App-Id": self._app_id}
        if self._token:
            h["X-Waygate-Token"] = self._token
        return h

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Connect to the Waygate Server, sync route states, start SSE listener."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(30.0),
        )
        # Auto-login: if credentials supplied but no pre-issued token, obtain
        # an SDK-platform token now so the service never needs manual auth.
        if not self._token and self._username and self._password:
            await self._auto_login()
            # Re-create the client with the freshly obtained token in headers.
            await self._client.aclose()
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=httpx.Timeout(30.0),
            )
        await self._sync_from_server()
        self._sse_task = asyncio.create_task(
            self._sse_loop(),
            name=f"waygate-server-backend-sse[{self._app_id}]",
        )

    async def shutdown(self) -> None:
        """Cancel SSE listener and close the HTTP client."""
        if self._sse_task is not None:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sse_task
            self._sse_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Server sync
    async def _auto_login(self) -> None:
        """Exchange credentials for an SDK-platform token on startup.

        The token is stored in ``self._token`` so subsequent requests
        include the ``X-Waygate-Token`` header automatically.  Logs a
        warning (does not raise) if the login fails so the service starts
        in fail-open mode rather than crashing.
        """
        assert self._client is not None
        try:
            resp = await self._client.post(
                "/api/auth/login",
                json={
                    "username": self._username,
                    "password": self._password,
                    "platform": "sdk",
                },
            )
            if resp.status_code == 200:
                self._token = resp.json().get("token")
                logger.info("WaygateServerBackend[%s]: auto-login succeeded", self._app_id)
            else:
                logger.warning(
                    "WaygateServerBackend[%s]: auto-login failed (%s) — "
                    "proceeding unauthenticated (fail-open)",
                    self._app_id,
                    resp.status_code,
                )
        except Exception:
            logger.warning(
                "WaygateServerBackend[%s]: auto-login request failed — "
                "proceeding unauthenticated (fail-open)",
                self._app_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------

    async def _sync_from_server(self) -> None:
        """Pull all current route states and RL policies from the Waygate Server.

        Fail-open: a failed sync logs a warning and leaves the cache
        empty.  Requests still flow through (no state = active by default
        via the engine's fail-open behaviour).
        """
        assert self._client is not None
        try:
            resp = await self._client.get("/api/routes", params={"service": self._app_id})
            resp.raise_for_status()
            for state_dict in resp.json():
                state = RouteState.model_validate(state_dict)
                local_key = self._local_path(state)
                self._cache[local_key] = state
            logger.info(
                "WaygateServerBackend[%s]: synced %d route(s) from %s",
                self._app_id,
                len(self._cache),
                self._base_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WaygateServerBackend[%s]: initial sync from %s failed — "
                "starting with empty cache (%s). Requests will pass through.",
                self._app_id,
                self._base_url,
                exc,
            )

        # Also sync rate limit policies so they are available immediately.
        try:
            resp = await self._client.get("/api/rate-limits")
            resp.raise_for_status()
            for policy_dict in resp.json():
                path = policy_dict.get("path", "")
                method = policy_dict.get("method", "GET")
                key = f"{method.upper()}:{path}"
                self._rl_policy_cache[key] = policy_dict
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WaygateServerBackend[%s]: RL policy sync failed (non-fatal): %s",
                self._app_id,
                exc,
            )

    async def _flush_pending(self) -> None:
        """Push locally-registered new routes to the Waygate Server as one batch.

        Called by :class:`~waygate.sdk.WaygateSDK` after route discovery at
        startup so the dashboard reflects the service's routes.  Routes
        already present on the server are left untouched (server-wins).

        Sets :attr:`_startup_done` to ``True`` after the flush (whether or not
        the HTTP request succeeded) so that subsequent :meth:`set_state` calls
        — from runtime mutations like ``engine.disable()`` — are pushed
        immediately instead of being queued.
        """
        try:
            if not self._pending or self._client is None:
                return
            batch = list(self._pending.values())
            self._pending.clear()
            try:
                await self._client.post(
                    "/api/sdk/register",
                    json={
                        "app_id": self._app_id,
                        "states": [s.model_dump(mode="json") for s in batch],
                    },
                )
                logger.debug(
                    "WaygateServerBackend[%s]: registered %d new route(s) with server",
                    self._app_id,
                    len(batch),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WaygateServerBackend[%s]: failed to register %d route(s) with server: %s",
                    self._app_id,
                    len(batch),
                    exc,
                )
        finally:
            # Mark startup complete so runtime set_state() calls push immediately.
            self._startup_done = True

    # ------------------------------------------------------------------
    # SSE listener
    # ------------------------------------------------------------------

    async def _sse_loop(self) -> None:
        """Maintain the SSE connection; reconnect automatically on any drop."""
        while True:
            try:
                await self._listen_sse()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WaygateServerBackend[%s]: SSE disconnected (%s), reconnecting in %.1fs",
                    self._app_id,
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                # Re-sync the full state after reconnecting in case we
                # missed updates while the connection was down.
                await self._sync_from_server()

    @staticmethod
    def _local_path(state: RouteState) -> str:
        """Return the plain local path used as the enforcement cache key.

        Routes registered by this SDK are stored on the Waygate Server with
        a service-prefixed path (``"payments-service:/api/payments"``).
        Strip the prefix so ``engine.check("/api/payments")`` resolves
        correctly against the local cache.
        """
        if state.service and state.path.startswith(f"{state.service}:"):
            return state.path[len(state.service) + 1 :]
        return state.path

    async def _listen_sse(self) -> None:
        """Connect to /api/sdk/events and update caches on each typed JSON event."""
        import json as _json

        if self._client is None:
            return
        async with self._client.stream("GET", "/api/sdk/events") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    envelope = _json.loads(data)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "WaygateServerBackend[%s]: failed to parse SSE payload: %s",
                        self._app_id,
                        exc,
                    )
                    continue

                event_type = envelope.get("type") if isinstance(envelope, dict) else None

                if event_type == "state":
                    # Typed state envelope from a new-format server.
                    payload = envelope.get("payload", {})
                    try:
                        state = RouteState.model_validate(payload)
                    except Exception:
                        continue
                    if state.service and state.service != self._app_id:
                        continue
                    local_key = self._local_path(state)
                    self._cache[local_key] = state
                    logger.debug(
                        "WaygateServerBackend[%s]: cache updated — %s → %s",
                        self._app_id,
                        local_key,
                        state.status,
                    )
                    # Notify state subscribers so _run_route_state_listener
                    # bumps _schema_version, which invalidates the OpenAPI cache.
                    _svc_global_key = f"__waygate:svc_global:{self._app_id}__"
                    if local_key not in (_GLOBAL_KEY, _svc_global_key):
                        for q in self._state_subscribers:
                            q.put_nowait(state)
                    # Notify engine to invalidate global-maintenance cache when
                    # the all-services global config OR this service's own
                    # maintenance config arrives via SSE.
                    else:
                        for gc_q in self._global_config_changed:
                            gc_q.put_nowait(None)

                elif event_type == "rl_policy":
                    # Rate limit policy change.
                    action = envelope.get("action")
                    key = envelope.get("key", "")  # "METHOD:local_path"
                    if action == "set":
                        policy = envelope.get("policy", {})
                        self._rl_policy_cache[key] = policy
                        event: dict[str, Any] = {"action": "set", "key": key, "policy": policy}
                        for rl_q in self._rl_policy_subscribers:
                            rl_q.put_nowait(event)
                        logger.debug(
                            "WaygateServerBackend[%s]: RL policy set — %s", self._app_id, key
                        )
                    elif action == "delete":
                        self._rl_policy_cache.pop(key, None)
                        del_event: dict[str, Any] = {"action": "delete", "key": key}
                        for rl_q in self._rl_policy_subscribers:
                            rl_q.put_nowait(del_event)
                        logger.debug(
                            "WaygateServerBackend[%s]: RL policy deleted — %s", self._app_id, key
                        )

                elif event_type == "flag_updated":
                    key = envelope.get("key", "")
                    flag_data = envelope.get("flag")
                    if key and flag_data is not None:
                        self._flag_cache[key] = flag_data
                        flag_event: dict[str, Any] = {
                            "type": "flag_updated",
                            "key": key,
                            "flag": flag_data,
                        }
                        for flag_q in self._flag_subscribers:
                            flag_q.put_nowait(flag_event)
                        logger.debug(
                            "WaygateServerBackend[%s]: flag cache updated — %s",
                            self._app_id,
                            key,
                        )

                elif event_type == "flag_deleted":
                    key = envelope.get("key", "")
                    if key:
                        self._flag_cache.pop(key, None)
                        flag_del_event: dict[str, Any] = {"type": "flag_deleted", "key": key}
                        for flag_q in self._flag_subscribers:
                            flag_q.put_nowait(flag_del_event)
                        logger.debug(
                            "WaygateServerBackend[%s]: flag deleted — %s", self._app_id, key
                        )

                elif event_type == "segment_updated":
                    key = envelope.get("key", "")
                    seg_data = envelope.get("segment")
                    if key and seg_data is not None:
                        self._segment_cache[key] = seg_data
                        seg_event: dict[str, Any] = {
                            "type": "segment_updated",
                            "key": key,
                            "segment": seg_data,
                        }
                        for flag_q in self._flag_subscribers:
                            flag_q.put_nowait(seg_event)
                        logger.debug(
                            "WaygateServerBackend[%s]: segment cache updated — %s",
                            self._app_id,
                            key,
                        )

                elif event_type == "segment_deleted":
                    key = envelope.get("key", "")
                    if key:
                        self._segment_cache.pop(key, None)
                        seg_del_event: dict[str, Any] = {"type": "segment_deleted", "key": key}
                        for flag_q in self._flag_subscribers:
                            flag_q.put_nowait(seg_del_event)
                        logger.debug(
                            "WaygateServerBackend[%s]: segment deleted — %s", self._app_id, key
                        )

                else:
                    # Legacy plain-RouteState payload (old server without typed envelopes).
                    try:
                        state = RouteState.model_validate(envelope)
                        if state.service and state.service != self._app_id:
                            continue
                        local_key = self._local_path(state)
                        self._cache[local_key] = state
                        for q in self._state_subscribers:
                            q.put_nowait(state)
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # WaygateBackend ABC — core state operations
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return cached state — zero network hop."""
        try:
            return self._cache[path]
        except KeyError:
            raise KeyError(f"No state registered for {path!r}") from None

    async def set_state(self, path: str, state: RouteState) -> None:
        """Update local cache immediately; push to Waygate Server asynchronously.

        Sets ``state.service`` to this SDK's ``app_id`` and stores with
        the service-prefixed path on the Waygate Server so routes from
        different services never collide.  The local cache always uses the
        plain path so ``engine.check()`` resolves correctly.

        During startup (before the HTTP client exists) the state is queued
        in ``_pending`` and flushed via :meth:`_flush_pending` once the
        client is ready.  At runtime, a background task fires the push
        without blocking the caller.
        """
        # Tag with this service and build the namespaced server-side path.
        state = state.model_copy(
            update={
                "service": self._app_id,
                "path": f"{self._app_id}:{path}",
            }
        )
        # Local cache always uses the plain path for zero-overhead enforcement.
        self._cache[path] = state
        if self._client is None or not self._startup_done:
            # Queue for batch flush: use server-side path as key so duplicate
            # registrations (e.g. from both WaygateRouter and SDK scan) collapse
            # into one entry — latest state wins.
            self._pending[state.path] = state
        else:
            asyncio.create_task(self._push_state(state))

    async def _push_state(self, state: RouteState) -> None:
        if self._client is None:
            return
        try:
            await self._client.post(
                "/api/sdk/register",
                json={
                    "app_id": self._app_id,
                    "states": [state.model_dump(mode="json")],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WaygateServerBackend[%s]: failed to push state for %s: %s",
                self._app_id,
                state.path,
                exc,
            )

    async def delete_state(self, path: str) -> None:
        self._cache.pop(path, None)

    async def list_states(self) -> list[RouteState]:
        # Exclude sentinel keys used for global/service maintenance configs
        # so they don't appear as regular routes in the dashboard or CLI.
        #
        # Normalize state.path to the local (app-side) cache key — e.g.
        # "GET:/api/payments" — rather than the server-side namespaced path
        # "payments-service:GET:/api/payments".  Callers such as
        # apply_waygate_to_openapi look up states by the local path that
        # matches FastAPI's route paths, so the paths must align.
        result: list[RouteState] = []
        for local_key, state in self._cache.items():
            if local_key.startswith("__waygate:"):
                continue
            if state.path != local_key:
                state = state.model_copy(update={"path": local_key})
            result.append(state)
        return result

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def write_audit(self, entry: AuditEntry) -> None:
        """Forward audit entry to the Waygate Server (fire-and-forget)."""
        if self._client is not None:
            asyncio.create_task(self._push_audit(entry))

    async def _push_audit(self, entry: AuditEntry) -> None:
        if self._client is None:
            return
        try:
            await self._client.post(
                "/api/sdk/audit",
                json=entry.model_dump(mode="json"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "WaygateServerBackend[%s]: failed to push audit entry: %s",
                self._app_id,
                exc,
            )

    async def get_audit_log(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Fetch audit log from the Waygate Server."""
        if self._client is None:
            return []
        try:
            params: dict[str, Any] = {"limit": limit}
            if path:
                params["route"] = path
            resp = await self._client.get("/api/audit", params=params)
            resp.raise_for_status()
            return [AuditEntry.model_validate(e) for e in resp.json()]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WaygateServerBackend[%s]: failed to fetch audit log: %s",
                self._app_id,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # subscribe() — yields state changes received via the SSE connection
    # ------------------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[RouteState]:
        """Yield every route-state change pushed by the Waygate Server via SSE.

        State changes arrive through the existing ``/api/sdk/events`` SSE
        connection managed by :meth:`_listen_sse`.  Each yielded
        ``RouteState`` is a normal (non-sentinel) route update for this
        service.

        The engine's ``_run_route_state_listener`` consumes this generator
        and calls ``_bump_schema_version()`` on every yield so that
        ``apply_waygate_to_openapi``'s cache is invalidated and ``/docs``
        reflects live state changes made via the dashboard or CLI.
        """
        q: asyncio.Queue[RouteState] = asyncio.Queue()
        self._state_subscribers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            with contextlib.suppress(ValueError):
                self._state_subscribers.remove(q)

    # ------------------------------------------------------------------
    # Global maintenance — SSE-driven cache invalidation
    # ------------------------------------------------------------------

    async def subscribe_global_config(self) -> AsyncIterator[None]:
        """Yield whenever the global or service-specific maintenance config changes.

        The signal arrives via the SDK's existing SSE connection: when
        :meth:`_listen_sse` receives a state update for the global-config
        sentinel key (``_GLOBAL_KEY``) or this service's own maintenance key,
        it puts ``None`` into every queue registered here.  The engine
        consumes these signals to invalidate its in-process config cache.
        """
        q: asyncio.Queue[None] = asyncio.Queue()
        self._global_config_changed.append(q)
        try:
            while True:
                await q.get()
                yield None
        finally:
            try:
                self._global_config_changed.remove(q)
            except ValueError:
                pass

    async def get_global_config(self) -> GlobalMaintenanceConfig:
        """Return the effective global-maintenance config for this service.

        Checks (in order):
        1. All-services global maintenance (``_GLOBAL_KEY``) — if enabled,
           return immediately (highest priority).
        2. Service-specific maintenance (``__waygate:svc_global:<app_id>__``)
           — allows per-service emergency maintenance without touching all
           other services.

        Both configs are read from the local SSE-driven cache so there is
        no network hop per request.
        """
        # 1. All-services global config
        try:
            state = self._cache.get(_GLOBAL_KEY)
            if state is not None:
                cfg = GlobalMaintenanceConfig.model_validate_json(state.reason)
                if cfg.enabled:
                    return cfg
        except Exception:  # noqa: BLE001
            pass
        # 2. Service-specific maintenance config
        svc_key = f"__waygate:svc_global:{self._app_id}__"
        try:
            state = self._cache.get(svc_key)
            if state is not None:
                return GlobalMaintenanceConfig.model_validate_json(state.reason)
        except Exception:  # noqa: BLE001
            pass
        return GlobalMaintenanceConfig()

    # ------------------------------------------------------------------
    # Route deduplication helper
    # ------------------------------------------------------------------

    async def get_registered_paths(self) -> set[str]:
        """Return local cache keys for route-deduplication in register_batch.

        Unlike the default implementation (which uses state ``.path`` fields
        that include the service prefix), this returns the local cache keys —
        i.e. plain paths like ``"GET:/api/payments"`` or ``"/api/payments"``.
        This lets ``engine.register_batch()`` correctly detect routes that
        are already known to this SDK instance, regardless of whether they
        were synced from the server with or without a method prefix.

        The returned set also includes bare-path variants (method prefix
        stripped) so that routes registered by ``WaygateRouter`` as
        ``"GET:/api/payments"`` are matched against SDK-discovered paths of
        ``"/api/payments"``.
        """
        # Exclude sentinel keys (global config, service maintenance) so they
        # are never treated as real routes in register_batch dedup checks.
        keys = {k for k in self._cache.keys() if not k.startswith("__waygate:")}
        bare: set[str] = set()
        _methods = ("GET:", "POST:", "PUT:", "PATCH:", "DELETE:", "HEAD:", "OPTIONS:")
        for key in keys:
            for m in _methods:
                if key.startswith(m):
                    bare.add(key[len(m) :])
                    break
        return keys | bare

    # ------------------------------------------------------------------
    # Rate limit policy — local cache, updated via SSE
    # ------------------------------------------------------------------

    async def set_rate_limit_policy(
        self, path: str, method: str, policy_data: dict[str, Any]
    ) -> None:
        """Update local RL policy cache (actual storage lives on the Waygate Server)."""
        key = f"{method.upper()}:{path}"
        self._rl_policy_cache[key] = policy_data

    async def get_rate_limit_policies(self) -> list[dict[str, Any]]:
        """Return all RL policies known to this SDK instance."""
        return list(self._rl_policy_cache.values())

    async def delete_rate_limit_policy(self, path: str, method: str) -> None:
        """Remove an RL policy from local cache."""
        self._rl_policy_cache.pop(f"{method.upper()}:{path}", None)

    async def subscribe_rate_limit_policy(self) -> AsyncIterator[dict[str, Any]]:
        """Yield rate limit policy change events pushed via the SSE connection.

        Events have the same shape as ``MemoryBackend.subscribe_rate_limit_policy()``:
        ``{"action": "set/delete", "key": "METHOD:path", "policy": {...}}``
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._rl_policy_subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(ValueError):
                self._rl_policy_subscribers.remove(queue)

    async def subscribe_flag_changes(self) -> AsyncIterator[dict[str, Any]]:
        """Yield feature flag / segment change events pushed via the SSE connection.

        Each yielded dict has one of these shapes::

            {"type": "flag_updated",    "key": "my-flag",  "flag": {...}}
            {"type": "flag_deleted",    "key": "my-flag"}
            {"type": "segment_updated", "key": "my-seg",   "segment": {...}}
            {"type": "segment_deleted", "key": "my-seg"}
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._flag_subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(ValueError):
                self._flag_subscribers.remove(queue)

    # ------------------------------------------------------------------
    # Feature flag storage — returns locally cached data fetched via SSE
    # ------------------------------------------------------------------

    async def load_all_flags(self) -> list[Any]:
        """Return all feature flags cached from the Waygate Server."""
        return list(self._flag_cache.values())

    async def load_all_segments(self) -> list[Any]:
        """Return all segments cached from the Waygate Server."""
        return list(self._segment_cache.values())
