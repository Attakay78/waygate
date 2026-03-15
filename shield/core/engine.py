"""ShieldEngine — the central orchestrator for api-shield.

All business logic lives here. Middleware and decorators are transport
layers that call into the engine. They never make state decisions themselves.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from shield.core.backends.base import ShieldBackend
from shield.core.backends.memory import MemoryBackend
from shield.core.exceptions import (
    AmbiguousRouteError,
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
    RouteNotFoundException,
    RouteProtectedException,
)
from shield.core.models import (
    AuditEntry,
    GlobalMaintenanceConfig,
    MaintenanceWindow,
    RouteState,
    RouteStatus,
)
from shield.core.webhooks import default_formatter

logger = logging.getLogger(__name__)

# Type alias for a webhook formatter callable.
WebhookFormatter = Callable[[str, str, RouteState], dict[str, Any]]


class ShieldEngine:
    """Central orchestrator — all route lifecycle logic flows through here.

    Parameters
    ----------
    backend:
        Storage backend. Defaults to ``MemoryBackend``.
    current_env:
        Name of the current runtime environment (e.g. ``"dev"``).
        Used to evaluate ``ENV_GATED`` route restrictions.
    """

    def __init__(
        self,
        backend: ShieldBackend | None = None,
        current_env: str = "dev",
    ) -> None:
        self.backend: ShieldBackend = backend or MemoryBackend()
        self.current_env = current_env
        # Scheduler is lazily imported to avoid a circular reference.
        from shield.core.scheduler import MaintenanceScheduler

        self.scheduler: MaintenanceScheduler = MaintenanceScheduler(engine=self)
        # Webhook registry: list of (url, formatter) pairs.
        self._webhooks: list[tuple[str, WebhookFormatter]] = []
        # Global config cache — avoids a backend round-trip on every request.
        # Invalidated whenever the global config is written in this process.
        # Acceptable stale window for multi-instance deployments: until the
        # next write in this process (usually a human-initiated action).
        self._global_config_cache: GlobalMaintenanceConfig | None = None
        # Monotonic counter bumped on every state change.  Used by the OpenAPI
        # filter to detect when the cached schema needs to be rebuilt.
        self._schema_version: int = 0

    # ------------------------------------------------------------------
    # Async context manager — calls backend lifecycle hooks
    # ------------------------------------------------------------------

    async def __aenter__(self) -> ShieldEngine:
        """Call ``backend.startup()`` and return *self*.

        Use ``async with ShieldEngine(...) as engine:`` to ensure the
        backend is initialised before use and cleanly shut down afterwards.
        This is the recommended pattern for CLI scripts and custom backends
        that require async setup (e.g. opening a database connection).
        """
        await self.backend.startup()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Call ``backend.shutdown()`` regardless of whether an exception occurred."""
        await self.backend.shutdown()

    # ------------------------------------------------------------------
    # Hot-path: check
    # ------------------------------------------------------------------

    async def check(
        self,
        path: str,
        method: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Evaluate the lifecycle policy for *path* (and optionally *method*).

        This is the single chokepoint for every incoming request.
        It raises a ``ShieldException`` subclass when the request should
        be blocked, and returns ``None`` when the request may proceed.

        Resolution order (highest priority first):
        1. Global maintenance (if enabled and path not in ``exempt_paths``)
        2. Method-specific state:  ``"GET:/payments"``
        3. Path-level state:       ``"/payments"``  (all methods)
        4. No state found → ACTIVE (fail-open)

        Fail-open: if the backend is unreachable, the error is logged and
        the request is allowed through — shield never takes down an API.

        Parameters
        ----------
        path:
            The URL path being requested.
        method:
            HTTP method (``"GET"``, ``"POST"``, …).  When provided the engine
            checks for a method-specific state before falling back to the
            path-level state.
        context:
            Optional dict with request metadata (``ip``, ``headers``, etc.).
        """
        # 1. Global maintenance check — highest priority.
        try:
            global_cfg = await self._get_global_config_cached()
            if global_cfg.enabled:
                method_key = f"{method.upper()}:{path}" if method else None
                # Use frozenset for O(1) membership tests instead of O(M) list scan.
                exempt = global_cfg.exempt_set
                is_exempt = path in exempt or (method_key is not None and method_key in exempt)
                if not is_exempt:
                    raise MaintenanceException(reason=global_cfg.reason)
        except MaintenanceException:
            raise
        except Exception:
            logger.exception("shield: backend error reading global config — failing open")

        # 2. Per-route state check.
        state = await self._resolve_state(path, method)
        if state is None:
            return  # no state registered → effectively ACTIVE

        if state.status == RouteStatus.ACTIVE:
            return

        if state.status == RouteStatus.MAINTENANCE:
            retry_after = state.window.end if state.window else None
            raise MaintenanceException(reason=state.reason, retry_after=retry_after)

        if state.status == RouteStatus.DISABLED:
            raise RouteDisabledException(reason=state.reason)

        if state.status == RouteStatus.ENV_GATED:
            if self.current_env not in state.allowed_envs:
                raise EnvGatedException(
                    path=path,
                    current_env=self.current_env,
                    allowed_envs=state.allowed_envs,
                )
            return

        if state.status == RouteStatus.DEPRECATED:
            # Deprecated routes still serve requests — headers injected by middleware.
            return

    async def _resolve_state(self, path: str, method: str | None) -> RouteState | None:
        """Return the applicable ``RouteState`` for *path* / *method*.

        Checks method-specific state first, then falls back to path-level.
        Returns ``None`` on *KeyError* (no state = ACTIVE) or backend errors
        (fail-open).
        """
        candidates = []
        if method:
            candidates.append(f"{method.upper()}:{path}")
        candidates.append(path)

        for key in candidates:
            try:
                return await self.backend.get_state(key)
            except KeyError:
                continue
            except Exception:
                logger.exception("shield: backend error reading state for %r — failing open", key)
                return None  # fail-open

        return None  # no state found

    async def route_exists(self, key: str) -> bool:
        """Return ``True`` if *key* has a registered state in the backend.

        *key* may be a bare path (``"/payments"``) or a method-prefixed key
        (``"GET:/payments"``).  Used by the CLI to validate operations before
        applying them.
        """
        try:
            await self.backend.get_state(key)
            return True
        except KeyError:
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Registration (called by ShieldRouter at startup)
    # ------------------------------------------------------------------

    async def register(self, path: str, meta: dict[str, Any]) -> None:
        """Register a route from its ``__shield_meta__`` decorator stamp.

        Called by ``ShieldRouter`` at application startup for every route
        that carries shield metadata.

        **Persistence-first:** if the backend already has a state entry for
        *path* (written by a previous CLI command or a prior server run), that
        entry is left untouched.  The decorator provides the *initial* state
        only — runtime changes made via ``engine.disable()``, the CLI, or the
        dashboard survive server restarts.

        For ``MemoryBackend`` the backend is always empty at startup, so
        decorator state is always applied (no persistence to respect).
        """
        # If a persisted state exists, honour it and skip re-registration.
        try:
            await self.backend.get_state(path)
            return  # already persisted — runtime state wins over decorator
        except KeyError:
            pass  # no existing state — fall through and register from decorator

        is_force_active = bool(meta.get("force_active"))
        status_str: str = meta.get("status", RouteStatus.ACTIVE)
        status = RouteStatus(status_str)

        # Build initial state from decorator metadata.
        state = RouteState(
            path=path,
            status=status,
            reason=meta.get("reason", ""),
            allowed_envs=meta.get("allowed_envs", []),
            sunset_date=meta.get("sunset_date"),
            successor_path=meta.get("successor_path"),
            force_active=is_force_active,
        )

        if "window" in meta and meta["window"] is not None:
            state.window = meta["window"]

        await self.backend.set_state(path, state)

    async def register_batch(self, routes: list[tuple[str, dict[str, Any]]]) -> None:
        """Register multiple routes in a single backend round-trip.

        Replaces N individual ``register()`` calls (each doing one
        ``backend.get_state()`` read) with a single ``backend.list_states()``
        call to discover already-persisted routes, then only writes the truly
        new ones.  For ``FileBackend`` this means one file read instead of N,
        and the debounced writer coalesces all new-state writes into a single
        disk flush.

        Like ``register()``, persisted state always wins over decorator state —
        routes already present in the backend are left untouched.

        Parameters
        ----------
        routes:
            Sequence of ``(path, meta)`` pairs exactly as accumulated by
            ``ShieldRouter._shield_routes``.
        """
        if not routes:
            return

        # One backend call to discover every already-persisted route.
        try:
            existing = await self.backend.list_states()
            existing_keys: set[str] = {s.path for s in existing}
        except Exception:
            logger.exception(
                "shield: register_batch — failed to list existing states, "
                "falling back to per-route registration"
            )
            for path, meta in routes:
                await self.register(path, meta)
            return

        for path, meta in routes:
            if path in existing_keys:
                continue  # persisted state wins — skip

            is_force_active = bool(meta.get("force_active"))
            status_str: str = meta.get("status", RouteStatus.ACTIVE)
            status = RouteStatus(status_str)

            state = RouteState(
                path=path,
                status=status,
                reason=meta.get("reason", ""),
                allowed_envs=meta.get("allowed_envs", []),
                sunset_date=meta.get("sunset_date"),
                successor_path=meta.get("successor_path"),
                force_active=is_force_active,
            )
            if "window" in meta and meta["window"] is not None:
                state.window = meta["window"]

            await self.backend.set_state(path, state)

    # ------------------------------------------------------------------
    # State mutation methods
    # ------------------------------------------------------------------

    async def _resolve_existing(self, path: str) -> RouteState:
        """Return the registered state for *path*, raising if not found or ambiguous.

        Unlike :meth:`_get_or_create` this method is intended for **mutation**
        operations.  It refuses to create phantom entries and surfaces ambiguity
        so the caller (CLI / API handler) can ask the user to be more specific.

        Resolution rules:

        * Exact key found → return it.
        * Bare path (no ``":"``), exactly one method-prefixed match →
          return that match transparently.
        * Bare path, no matches → raise :exc:`RouteNotFoundException`.
        * Bare path, two or more matches → raise :exc:`AmbiguousRouteError`.
        * Method-prefixed key not found → raise :exc:`RouteNotFoundException`.

        Raises
        ------
        RouteNotFoundException
            When *path* is not registered in the backend.
        AmbiguousRouteError
            When a bare *path* matches more than one method-prefixed route.
        """
        try:
            return await self.backend.get_state(path)
        except KeyError:
            pass

        # Bare path not found — check for method-prefixed variants.
        if ":" not in path:
            try:
                all_states = await self.backend.list_states()
                matches = [s for s in all_states if s.path.endswith(f":{path}")]
            except Exception:
                matches = []

            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AmbiguousRouteError(path, [s.path for s in matches])

        raise RouteNotFoundException(path)

    async def _assert_mutable(self, path: str) -> RouteState:
        """Return the current state, raising if the route cannot be mutated.

        Raises :exc:`RouteNotFoundException` when *path* is not registered,
        :exc:`AmbiguousRouteError` when a bare path matches multiple routes,
        and :exc:`RouteProtectedException` when the route is decorated with
        ``@force_active``.

        Called at the top of every mutation method so that ``@force_active``
        routes can never have their lifecycle state changed — not by the CLI,
        dashboard, or any direct engine call.
        """
        state = await self._resolve_existing(path)
        if state.force_active:
            raise RouteProtectedException(path)
        return state

    async def _get_global_config_cached(self) -> GlobalMaintenanceConfig:
        """Return the global config, using the in-process cache when available.

        The cache is populated on first call and invalidated whenever this
        process writes a new global config (enable/disable/set_exempt_paths).
        For single-instance deployments the cache is always fresh.  For
        multi-instance Redis deployments, cross-process changes are visible
        on the next write from *this* process — an acceptable tradeoff given
        that global maintenance is a rare, operator-initiated action.
        """
        if self._global_config_cache is not None:
            return self._global_config_cache
        cfg = await self.backend.get_global_config()
        self._global_config_cache = cfg
        return cfg

    def _invalidate_global_config_cache(self) -> None:
        """Drop the cached global config so the next check re-fetches from backend."""
        self._global_config_cache = None

    def _bump_schema_version(self) -> None:
        """Increment the schema version counter to signal that cached OpenAPI schemas
        are stale and need to be rebuilt on the next ``/docs`` or ``/openapi.json`` request.
        """
        self._schema_version += 1

    async def enable(
        self, path: str, actor: str = "system", reason: str = "", platform: str = "system"
    ) -> RouteState:
        """Enable *path*, returning the updated ``RouteState``.

        Parameters
        ----------
        path:
            The route key to enable (e.g. ``"GET:/payments"``).
        actor:
            Identity of the caller, written to the audit log.
        reason:
            Optional note explaining why the route is being re-enabled
            (e.g. ``"Migration complete"``).  Stored in the audit entry and
            on the route state so the reason is visible in ``shield status``.
        """
        old_state = await self._assert_mutable(path)
        actual_path = old_state.path  # may differ from path if bare path was resolved
        new_state = old_state.model_copy(
            update={"status": RouteStatus.ACTIVE, "reason": reason, "window": None}
        )
        await self.backend.set_state(actual_path, new_state)
        self._bump_schema_version()
        await self._audit(
            path=actual_path,
            action="enable",
            actor=actor,
            reason=reason,
            platform=platform,
            previous_status=old_state.status,
            new_status=RouteStatus.ACTIVE,
        )
        self._fire_webhooks("enable", actual_path, new_state)
        return new_state

    async def disable(
        self, path: str, reason: str = "", actor: str = "system", platform: str = "system"
    ) -> RouteState:
        """Disable *path* permanently, returning the updated ``RouteState``."""
        old_state = await self._assert_mutable(path)
        actual_path = old_state.path
        new_state = old_state.model_copy(update={"status": RouteStatus.DISABLED, "reason": reason})
        await self.backend.set_state(actual_path, new_state)
        self._bump_schema_version()
        await self._audit(
            path=actual_path,
            action="disable",
            actor=actor,
            reason=reason,
            platform=platform,
            previous_status=old_state.status,
            new_status=RouteStatus.DISABLED,
        )
        self._fire_webhooks("disable", actual_path, new_state)
        return new_state

    async def set_maintenance(
        self,
        path: str,
        reason: str = "",
        window: MaintenanceWindow | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> RouteState:
        """Put *path* into maintenance mode, returning the updated ``RouteState``."""
        old_state = await self._assert_mutable(path)
        actual_path = old_state.path
        new_state = old_state.model_copy(
            update={
                "status": RouteStatus.MAINTENANCE,
                "reason": reason,
                "window": window,
            }
        )
        await self.backend.set_state(actual_path, new_state)
        self._bump_schema_version()
        await self._audit(
            path=actual_path,
            action="maintenance_on",
            actor=actor,
            reason=reason,
            platform=platform,
            previous_status=old_state.status,
            new_status=RouteStatus.MAINTENANCE,
        )
        self._fire_webhooks("maintenance_on", actual_path, new_state)
        return new_state

    async def schedule_maintenance(
        self,
        path: str,
        window: MaintenanceWindow,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Schedule a future maintenance window for *path*.

        Delegates to ``self.scheduler``.  The window is persisted in the
        backend so it can be recovered after a restart.
        """
        # Persist the window immediately so restart recovery can find it.
        await self.set_maintenance(
            path, reason=window.reason, window=window, actor=actor, platform=platform
        )
        await self.scheduler.schedule(path, window, actor=actor)

    async def set_env_only(
        self, path: str, envs: list[str], actor: str = "system", platform: str = "system"
    ) -> RouteState:
        """Restrict *path* to *envs*, returning the updated ``RouteState``."""
        old_state = await self._assert_mutable(path)
        actual_path = old_state.path
        new_state = old_state.model_copy(
            update={"status": RouteStatus.ENV_GATED, "allowed_envs": envs}
        )
        await self.backend.set_state(actual_path, new_state)
        self._bump_schema_version()
        await self._audit(
            path=actual_path,
            action="env_gate",
            actor=actor,
            reason=f"Restricted to: {envs}",
            platform=platform,
            previous_status=old_state.status,
            new_status=RouteStatus.ENV_GATED,
        )
        return new_state

    # ------------------------------------------------------------------
    # Global maintenance
    # ------------------------------------------------------------------

    async def get_global_maintenance(self) -> GlobalMaintenanceConfig:
        """Return the current global maintenance configuration."""
        return await self.backend.get_global_config()

    async def enable_global_maintenance(
        self,
        reason: str = "",
        exempt_paths: list[str] | None = None,
        include_force_active: bool = False,
        actor: str = "system",
        platform: str = "system",
    ) -> GlobalMaintenanceConfig:
        """Enable global maintenance mode, blocking all non-exempt routes.

        Parameters
        ----------
        reason:
            Human-readable reason shown in every 503 response.
        exempt_paths:
            Route keys that bypass global maintenance.  Accepts bare paths
            (``"/health"``) and method-prefixed keys (``"GET:/health"``).
        include_force_active:
            When ``True``, ``@force_active`` routes are also blocked.
            Defaults to ``False`` (health checks remain reachable).
        actor:
            Identity of the caller, written to the audit log.
        """
        old_cfg = await self.backend.get_global_config()
        cfg = GlobalMaintenanceConfig(
            enabled=True,
            reason=reason,
            exempt_paths=exempt_paths or [],
            include_force_active=include_force_active,
        )
        await self.backend.set_global_config(cfg)
        self._invalidate_global_config_cache()
        self._bump_schema_version()
        prev = RouteStatus.MAINTENANCE if old_cfg.enabled else RouteStatus.ACTIVE
        await self._audit(
            path="__global__",
            action="global_maintenance_on",
            actor=actor,
            reason=reason,
            platform=platform,
            previous_status=prev,
            new_status=RouteStatus.MAINTENANCE,
        )
        return cfg

    async def disable_global_maintenance(
        self, actor: str = "system", platform: str = "system"
    ) -> GlobalMaintenanceConfig:
        """Disable global maintenance mode, restoring per-route state."""
        old_cfg = await self.backend.get_global_config()
        cfg = GlobalMaintenanceConfig(enabled=False)
        await self.backend.set_global_config(cfg)
        self._invalidate_global_config_cache()
        self._bump_schema_version()
        prev = RouteStatus.MAINTENANCE if old_cfg.enabled else RouteStatus.ACTIVE
        await self._audit(
            path="__global__",
            action="global_maintenance_off",
            actor=actor,
            platform=platform,
            previous_status=prev,
            new_status=RouteStatus.ACTIVE,
        )
        return cfg

    async def set_global_exempt_paths(
        self,
        paths: list[str],
    ) -> GlobalMaintenanceConfig:
        """Replace the exempt-paths list on the current global config."""
        cfg = await self.backend.get_global_config()
        updated = cfg.model_copy(update={"exempt_paths": paths})
        await self.backend.set_global_config(updated)
        self._invalidate_global_config_cache()
        self._bump_schema_version()
        return updated

    # ------------------------------------------------------------------
    # Webhook management
    # ------------------------------------------------------------------

    def add_webhook(
        self,
        url: str,
        formatter: WebhookFormatter | None = None,
    ) -> None:
        """Register a webhook URL to be notified on state changes.

        Parameters
        ----------
        url:
            HTTP(S) endpoint to POST state-change events to.
        formatter:
            Callable that converts ``(event, path, state)`` into a JSON-
            serialisable dict.  Defaults to :func:`default_formatter`.
            Use :class:`SlackWebhookFormatter` for Slack Incoming Webhooks.
        """
        self._webhooks.append((url, formatter or default_formatter))

    def _fire_webhooks(self, event: str, path: str, state: RouteState) -> None:
        """Schedule fire-and-forget POST to every registered webhook URL.

        Failures are logged and never propagated — a broken webhook must
        never affect the hot path.
        """
        for url, formatter in self._webhooks:
            payload = formatter(event, path, state)
            asyncio.create_task(
                self._post_webhook(url, payload),
                name=f"shield-webhook:{event}:{path}",
            )

    @staticmethod
    async def _post_webhook(url: str, payload: dict[str, Any]) -> None:
        """Send a single webhook POST.  Errors are logged, not raised."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json=payload)
        except Exception:
            logger.exception("shield: webhook POST to %r failed", url)

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    async def get_state(self, path: str) -> RouteState:
        """Return the current ``RouteState`` for *path*.

        Returns a default ACTIVE state if the path is not registered.
        """
        return await self._get_or_create(path)

    async def list_states(self) -> list[RouteState]:
        """Return all registered route states, excluding internal sentinel entries."""
        states = await self.backend.list_states()
        return [s for s in states if not s.path.startswith("__shield:")]

    async def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Return audit log entries, newest first."""
        return await self.backend.get_audit_log(path=path, limit=limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_create(self, path: str) -> RouteState:
        """Return existing state or a default ACTIVE state for *path*.

        Used for **read** operations only (e.g. :meth:`get_state`).
        Returns a synthetic ACTIVE state when *path* is not registered so
        that reads never raise — the caller sees an ACTIVE route rather than
        an error.

        For **mutation** operations use :meth:`_resolve_existing` instead,
        which raises :exc:`RouteNotFoundException` and
        :exc:`AmbiguousRouteError` rather than silently creating phantom
        entries.
        """
        try:
            return await self.backend.get_state(path)
        except KeyError:
            return RouteState(path=path)

    async def _audit(
        self,
        path: str,
        action: str,
        previous_status: RouteStatus,
        new_status: RouteStatus,
        actor: str = "system",
        reason: str = "",
        platform: str = "system",
    ) -> None:
        """Write an audit entry for a state change."""
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            path=path,
            action=action,
            actor=actor,
            platform=platform,
            reason=reason,
            previous_status=previous_status,
            new_status=new_status,
        )
        await self.backend.write_audit(entry)
