"""WaygateEngine — the central orchestrator for waygate.

All business logic lives here. Middleware and decorators are transport
layers that call into the engine. They never make state decisions themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

from waygate.core.backends.base import WaygateBackend
from waygate.core.backends.memory import MemoryBackend
from waygate.core.exceptions import (
    AmbiguousRouteError,
    EnvGatedException,
    MaintenanceException,
    RateLimitExceededException,
    RouteDisabledException,
    RouteNotFoundException,
    RouteProtectedException,
)
from waygate.core.models import (
    AuditEntry,
    GlobalMaintenanceConfig,
    MaintenanceWindow,
    RouteState,
    RouteStatus,
)
from waygate.core.webhooks import default_formatter

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Type alias for a webhook formatter callable.
WebhookFormatter = Callable[[str, str, RouteState], dict[str, Any]]


class _SyncProxy:
    """Synchronous façade over :class:`WaygateEngine`.

    Access via ``engine.sync`` from any sync context that runs inside an
    anyio worker thread — which is exactly what FastAPI does for every
    ``def`` (non-async) route handler and dependency.

    Uses ``anyio.from_thread.run()`` internally, the same mechanism the
    waygate decorators use, so no event-loop wiring is needed.

    Do **not** call from inside an ``async def`` — use ``await engine.*``
    directly there.

    Examples
    --------
    Sync route handler::

        @router.post("/admin/deploy")
        @force_active
        def deploy():  # FastAPI runs sync handlers in a worker thread
            engine.sync.disable("GET:/payments", reason="deploy in progress")
            run_migration()
            engine.sync.enable("GET:/payments")
            return {"deployed": True}

    Background thread::

        def nightly_job():
            engine.sync.set_maintenance("GET:/reports", reason="nightly rebuild")
            rebuild_reports()
            engine.sync.enable("GET:/reports")
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: WaygateEngine) -> None:
        self._engine = engine

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        import anyio.from_thread

        return anyio.from_thread.run(coro)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Route lifecycle
    # ------------------------------------------------------------------

    def enable(
        self, path: str, actor: str = "system", reason: str = "", platform: str = "system"
    ) -> RouteState:
        """Sync version of :meth:`WaygateEngine.enable`."""
        return self._run(self._engine.enable(path, actor=actor, reason=reason, platform=platform))

    def disable(
        self, path: str, reason: str = "", actor: str = "system", platform: str = "system"
    ) -> RouteState:
        """Sync version of :meth:`WaygateEngine.disable`."""
        return self._run(self._engine.disable(path, reason=reason, actor=actor, platform=platform))

    def set_maintenance(
        self,
        path: str,
        reason: str = "",
        window: MaintenanceWindow | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> RouteState:
        """Sync version of :meth:`WaygateEngine.set_maintenance`."""
        return self._run(
            self._engine.set_maintenance(
                path, reason=reason, window=window, actor=actor, platform=platform
            )
        )

    def schedule_maintenance(
        self,
        path: str,
        window: MaintenanceWindow,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Sync version of :meth:`WaygateEngine.schedule_maintenance`."""
        self._run(
            self._engine.schedule_maintenance(path, window=window, actor=actor, platform=platform)
        )

    def set_env_only(
        self, path: str, envs: list[str], actor: str = "system", platform: str = "system"
    ) -> RouteState:
        """Sync version of :meth:`WaygateEngine.set_env_only`."""
        return self._run(self._engine.set_env_only(path, envs=envs, actor=actor, platform=platform))

    # ------------------------------------------------------------------
    # Global maintenance
    # ------------------------------------------------------------------

    def get_global_maintenance(self) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.get_global_maintenance`."""
        return self._run(self._engine.get_global_maintenance())

    def enable_global_maintenance(
        self,
        reason: str = "",
        exempt_paths: list[str] | None = None,
        include_force_active: bool = False,
        actor: str = "system",
        platform: str = "system",
    ) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.enable_global_maintenance`."""
        return self._run(
            self._engine.enable_global_maintenance(
                reason=reason,
                exempt_paths=exempt_paths,
                include_force_active=include_force_active,
                actor=actor,
                platform=platform,
            )
        )

    def disable_global_maintenance(
        self, actor: str = "system", platform: str = "system"
    ) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.disable_global_maintenance`."""
        return self._run(self._engine.disable_global_maintenance(actor=actor, platform=platform))

    def set_global_exempt_paths(self, paths: list[str]) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.set_global_exempt_paths`."""
        return self._run(self._engine.set_global_exempt_paths(paths))

    def get_service_maintenance(self, service: str) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.get_service_maintenance`."""
        return self._run(self._engine.get_service_maintenance(service))

    def enable_service_maintenance(
        self,
        service: str,
        reason: str = "",
        exempt_paths: list[str] | None = None,
        include_force_active: bool = False,
        actor: str = "system",
        platform: str = "system",
    ) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.enable_service_maintenance`."""
        return self._run(
            self._engine.enable_service_maintenance(
                service=service,
                reason=reason,
                exempt_paths=exempt_paths,
                include_force_active=include_force_active,
                actor=actor,
                platform=platform,
            )
        )

    def disable_service_maintenance(
        self, service: str, actor: str = "system", platform: str = "system"
    ) -> GlobalMaintenanceConfig:
        """Sync version of :meth:`WaygateEngine.disable_service_maintenance`."""
        return self._run(
            self._engine.disable_service_maintenance(
                service=service, actor=actor, platform=platform
            )
        )

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def set_rate_limit_policy(
        self,
        path: str,
        method: str,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        burst: int = 0,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Sync version of :meth:`WaygateEngine.set_rate_limit_policy`."""
        return self._run(
            self._engine.set_rate_limit_policy(
                path,
                method,
                limit,
                algorithm=algorithm,
                key_strategy=key_strategy,
                burst=burst,
                actor=actor,
                platform=platform,
            )
        )

    def delete_rate_limit_policy(
        self, path: str, method: str, *, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.delete_rate_limit_policy`."""
        self._run(
            self._engine.delete_rate_limit_policy(path, method, actor=actor, platform=platform)
        )

    def get_rate_limit_hits(self, path: str | None = None, limit: int = 100) -> list[Any]:
        """Sync version of :meth:`WaygateEngine.get_rate_limit_hits`."""
        return self._run(self._engine.get_rate_limit_hits(path=path, limit=limit))

    def reset_rate_limit(
        self, path: str, method: str | None = None, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.reset_rate_limit`."""
        self._run(
            self._engine.reset_rate_limit(path, method=method, actor=actor, platform=platform)
        )

    def set_global_rate_limit(
        self,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        on_missing_key: str | None = None,
        burst: int = 0,
        exempt_routes: list[str] | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Sync version of :meth:`WaygateEngine.set_global_rate_limit`."""
        return self._run(
            self._engine.set_global_rate_limit(
                limit,
                algorithm=algorithm,
                key_strategy=key_strategy,
                on_missing_key=on_missing_key,
                burst=burst,
                exempt_routes=exempt_routes,
                actor=actor,
                platform=platform,
            )
        )

    def get_global_rate_limit(self) -> Any:
        """Sync version of :meth:`WaygateEngine.get_global_rate_limit`."""
        return self._run(self._engine.get_global_rate_limit())

    def delete_global_rate_limit(self, *, actor: str = "system", platform: str = "system") -> None:
        """Sync version of :meth:`WaygateEngine.delete_global_rate_limit`."""
        self._run(self._engine.delete_global_rate_limit(actor=actor, platform=platform))

    def reset_global_rate_limit(self, *, actor: str = "system", platform: str = "system") -> None:
        """Sync version of :meth:`WaygateEngine.reset_global_rate_limit`."""
        self._run(self._engine.reset_global_rate_limit(actor=actor, platform=platform))

    def enable_global_rate_limit(self, *, actor: str = "system", platform: str = "system") -> None:
        """Sync version of :meth:`WaygateEngine.enable_global_rate_limit`."""
        self._run(self._engine.enable_global_rate_limit(actor=actor, platform=platform))

    def disable_global_rate_limit(self, *, actor: str = "system", platform: str = "system") -> None:
        """Sync version of :meth:`WaygateEngine.disable_global_rate_limit`."""
        self._run(self._engine.disable_global_rate_limit(actor=actor, platform=platform))

    def get_service_rate_limit(self, service: str) -> Any:
        """Sync version of :meth:`WaygateEngine.get_service_rate_limit`."""
        return self._run(self._engine.get_service_rate_limit(service))

    def set_service_rate_limit(
        self,
        service: str,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        on_missing_key: str | None = None,
        burst: int = 0,
        exempt_routes: list[str] | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Sync version of :meth:`WaygateEngine.set_service_rate_limit`."""
        return self._run(
            self._engine.set_service_rate_limit(
                service,
                limit,
                algorithm=algorithm,
                key_strategy=key_strategy,
                on_missing_key=on_missing_key,
                burst=burst,
                exempt_routes=exempt_routes,
                actor=actor,
                platform=platform,
            )
        )

    def delete_service_rate_limit(
        self, service: str, *, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.delete_service_rate_limit`."""
        self._run(self._engine.delete_service_rate_limit(service, actor=actor, platform=platform))

    def reset_service_rate_limit(
        self, service: str, *, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.reset_service_rate_limit`."""
        self._run(self._engine.reset_service_rate_limit(service, actor=actor, platform=platform))

    def enable_service_rate_limit(
        self, service: str, *, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.enable_service_rate_limit`."""
        self._run(self._engine.enable_service_rate_limit(service, actor=actor, platform=platform))

    def disable_service_rate_limit(
        self, service: str, *, actor: str = "system", platform: str = "system"
    ) -> None:
        """Sync version of :meth:`WaygateEngine.disable_service_rate_limit`."""
        self._run(self._engine.disable_service_rate_limit(service, actor=actor, platform=platform))

    # ------------------------------------------------------------------
    # Read-only queries
    # ------------------------------------------------------------------

    def get_state(self, path: str) -> RouteState:
        """Sync version of :meth:`WaygateEngine.get_state`."""
        return self._run(self._engine.get_state(path))

    def list_states(self) -> list[RouteState]:
        """Sync version of :meth:`WaygateEngine.list_states`."""
        return self._run(self._engine.list_states())

    def get_audit_log(self, path: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """Sync version of :meth:`WaygateEngine.get_audit_log`."""
        return self._run(self._engine.get_audit_log(path=path, limit=limit))

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    @property
    def flag_client(self) -> Any:
        """Return the synchronous flag client, or ``None`` if flags are not active.

        Call ``engine.use_openfeature()`` first to activate the flag system.

        Since OpenFeature evaluation is CPU-bound, this client does **not**
        require a thread bridge — all methods are safe to call directly from
        a ``def`` handler running in an anyio worker thread.

        Example::

            @router.get("/checkout")
            def checkout(request: Request):
                enabled = engine.sync.flag_client.get_boolean_value(
                    "new_checkout", False, {"targeting_key": request.state.user_id}
                )
                return checkout_v2() if enabled else checkout_v1()
        """
        fc = self._engine._flag_client
        if fc is None:
            return None
        return fc.sync


class WaygateEngine:
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
        backend: WaygateBackend | None = None,
        current_env: str = "dev",
        rate_limit_backend: WaygateBackend | None = None,
        default_rate_limit_algorithm: Any = None,
        rate_limit_snapshot_interval: int = 10,
        max_rl_hit_entries: int = 10_000,
    ) -> None:
        self.backend: WaygateBackend = backend or MemoryBackend(
            max_rl_hit_entries=max_rl_hit_entries,
        )
        self.current_env = current_env
        # Scheduler is lazily imported to avoid a circular reference.
        from waygate.core.scheduler import MaintenanceScheduler

        self.scheduler: MaintenanceScheduler = MaintenanceScheduler(engine=self)
        # Webhook registry: list of (url, formatter) pairs.
        self._webhooks: list[tuple[str, WebhookFormatter]] = []
        # Global config cache — avoids a backend round-trip on every request.
        # Invalidated locally whenever this process writes a new config, and
        # invalidated remotely by the background listener task when another
        # instance publishes a change (RedisBackend only).
        self._global_config_cache: GlobalMaintenanceConfig | None = None
        # Background task that listens for cross-instance global config
        # invalidation signals (started by start(), cancelled by stop()).
        self._global_listener_task: asyncio.Task[None] | None = None
        # Background task that listens for route state changes from other
        # instances and bumps _schema_version so the OpenAPI schema cache is
        # invalidated on this worker (RedisBackend only).
        self._route_state_listener_task: asyncio.Task[None] | None = None
        # Background task that syncs rate limit policy changes from other
        # instances (RedisBackend only; no-op for Memory/File backends).
        self._rl_policy_listener_task: asyncio.Task[None] | None = None
        # Monotonic counter bumped on every state change.  Used by the OpenAPI
        # filter to detect when the cached schema needs to be rebuilt.
        self._schema_version: int = 0
        # Rate limiting — lazily initialised on first call to register_rate_limit().
        self._rate_limit_backend: WaygateBackend | None = rate_limit_backend
        self._rate_limit_snapshot_interval: int = rate_limit_snapshot_interval
        # Lazily set to a RateLimitAlgorithm enum value on first use.
        self._default_rate_limit_algorithm: Any = default_rate_limit_algorithm
        self._rate_limiter: Any = None  # WaygateRateLimiter | None
        self._rate_limit_policies: dict[str, Any] = {}  # "METHOD:/path" → RateLimitPolicy
        self._global_rate_limit_policy: Any = None  # GlobalRateLimitPolicy | None
        self._service_rate_limit_policies: dict[str, Any] = {}  # service → GlobalRateLimitPolicy
        # Sync proxy — created once, reused on every engine.sync access.
        self.sync: _SyncProxy = _SyncProxy(self)
        # Feature flags — lazily set by use_openfeature().
        self._flag_provider: Any = None  # WaygateOpenFeatureProvider | None
        self._flag_client: Any = None  # WaygateFeatureClient | None
        self._flag_scheduler: Any = None  # FlagScheduler | None (set by use_openfeature)

    # ------------------------------------------------------------------
    # Async context manager — calls backend lifecycle hooks
    # ------------------------------------------------------------------

    async def __aenter__(self) -> WaygateEngine:
        """Call ``backend.startup()``, start background tasks, and return *self*.

        Use ``async with WaygateEngine(...) as engine:`` to ensure the
        backend is initialised before use and cleanly shut down afterwards.
        This is the recommended pattern for CLI scripts and custom backends
        that require async setup (e.g. opening a database connection).
        """
        await self.backend.startup()
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Stop background tasks and call ``backend.shutdown()``."""
        await self.stop()
        await self.backend.shutdown()

    # ------------------------------------------------------------------
    # Distributed lifecycle — start / stop background tasks
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background tasks required for distributed operation.

        Starts three listeners when running with ``RedisBackend``:

        * ``waygate-global-config-listener`` — invalidates the in-process
          global maintenance config cache when another instance writes a
          new config, and bumps ``_schema_version`` so the OpenAPI schema
          cache is rebuilt on the next request.
        * ``waygate-route-state-listener`` — bumps ``_schema_version``
          whenever another instance changes any route state (enable,
          disable, maintenance, etc.) so this worker's OpenAPI schema
          cache is invalidated and rebuilt with the current state.
        * ``waygate-rl-policy-listener`` — syncs rate limit policy changes
          made on any instance into this instance's ``_rate_limit_policies``
          dict so every worker always enforces the current policy.

        All listeners exit silently when the backend raises
        ``NotImplementedError`` (``MemoryBackend``, ``FileBackend``).

        Safe to call multiple times — already-running tasks are left alone.
        """
        if self._global_listener_task is None or self._global_listener_task.done():
            self._global_listener_task = asyncio.create_task(
                self._run_global_config_listener(),
                name="waygate-global-config-listener",
            )
        if self._route_state_listener_task is None or self._route_state_listener_task.done():
            self._route_state_listener_task = asyncio.create_task(
                self._run_route_state_listener(),
                name="waygate-route-state-listener",
            )
        if self._rl_policy_listener_task is None or self._rl_policy_listener_task.done():
            self._rl_policy_listener_task = asyncio.create_task(
                self._run_rl_policy_listener(),
                name="waygate-rl-policy-listener",
            )
        if self._flag_provider is not None:
            # The OpenFeature SDK calls initialize() synchronously at
            # set_provider() time.  For async overrides the SDK silently
            # discards the coroutine; engine.start() detects and awaits it.
            # For sync initialize (including the base-class no-op) the SDK
            # already ran it, so we skip the redundant call and go straight
            # to warming the async backend cache.
            if asyncio.iscoroutinefunction(type(self._flag_provider).initialize):
                await self._flag_provider.initialize()
            else:
                await self._flag_provider._load_all()
        if self._flag_scheduler is not None:
            await self._flag_scheduler.start()

    async def stop(self) -> None:
        """Cancel background listener tasks and wait for them to finish.

        Called automatically by ``__aexit__``.  Safe to call when no
        tasks are running.
        """
        if self._global_listener_task is not None:
            self._global_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._global_listener_task
            self._global_listener_task = None
        if self._route_state_listener_task is not None:
            self._route_state_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._route_state_listener_task
            self._route_state_listener_task = None
        if self._rl_policy_listener_task is not None:
            self._rl_policy_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rl_policy_listener_task
            self._rl_policy_listener_task = None
        if self._flag_scheduler is not None:
            await self._flag_scheduler.stop()
        if self._flag_provider is not None:
            if asyncio.iscoroutinefunction(type(self._flag_provider).shutdown):
                await self._flag_provider.shutdown()
            else:
                self._flag_provider.shutdown()

    # ------------------------------------------------------------------
    # Feature flags — OpenFeature wiring
    # ------------------------------------------------------------------

    def use_openfeature(
        self,
        provider: Any = None,
        hooks: list[Any] | None = None,
        domain: str = "waygate",
    ) -> Any:
        """Activate the feature flag system backed by this engine's backend.

        Parameters
        ----------
        provider:
            An OpenFeature-compliant provider to use.  Defaults to
            ``WaygateOpenFeatureProvider(self.backend)`` — the built-in
            provider backed by the same backend as the engine.
        hooks:
            Additional OpenFeature hooks to register globally.  Default
            hooks (``LoggingHook``) are always added.
        domain:
            The OpenFeature domain name for the client.  Defaults to
            ``"waygate"``.

        Returns
        -------
        WaygateFeatureClient
            The feature client ready for flag evaluations.

        Raises
        ------
        ImportError
            When ``waygate[flags]`` is not installed.
        """
        from waygate.core.feature_flags._guard import _require_flags

        _require_flags()

        import openfeature.api as of_api
        from openfeature.hook import Hook

        from waygate.core.feature_flags.client import WaygateFeatureClient
        from waygate.core.feature_flags.hooks import LoggingHook
        from waygate.core.feature_flags.provider import WaygateOpenFeatureProvider

        if provider is None:
            provider = WaygateOpenFeatureProvider(self.backend)

        self._flag_provider = provider

        # Register the provider under the given domain (OpenFeature >=0.8 API).
        try:
            of_api.set_provider(provider, domain=domain)
        except TypeError:
            # Older openfeature-sdk versions without domain support.
            of_api.set_provider(provider)

        from waygate.core.feature_flags.hooks import MetricsHook

        metrics_hook = MetricsHook()

        # Build the default hook list and merge with any user-supplied hooks.
        default_hooks: list[Hook] = [LoggingHook(), metrics_hook]
        all_hooks = default_hooks + (hooks or [])
        of_api.add_hooks(all_hooks)

        # Create and cache the client.
        self._flag_client = WaygateFeatureClient(domain=domain)

        # Create the scheduler (start() is called later in engine.start()).
        from waygate.core.feature_flags.scheduler import FlagScheduler

        self._flag_scheduler = FlagScheduler(self)

        return self._flag_client

    @property
    def flag_client(self) -> Any:
        """Return the active ``WaygateFeatureClient``, or ``None`` if not configured.

        Call ``engine.use_openfeature()`` first to activate the flag system.
        """
        return self._flag_client

    @property
    def flag_scheduler(self) -> Any:
        """Return the active ``FlagScheduler``, or ``None`` if not configured."""
        return self._flag_scheduler

    # ------------------------------------------------------------------
    # Feature flag CRUD — single chokepoint for flag + segment operations
    # ------------------------------------------------------------------

    async def list_flags(self) -> list[Any]:
        """Return all feature flags from the provider cache (or backend)."""
        if self._flag_provider is not None:
            return list(self._flag_provider._flags.values())
        return await self.backend.load_all_flags()

    async def get_flag(self, key: str) -> Any:
        """Return a single ``FeatureFlag`` by *key*, or ``None`` if not found."""
        if self._flag_provider is not None:
            return self._flag_provider._flags.get(key)
        flags = await self.backend.load_all_flags()
        return next((f for f in flags if f.key == key), None)

    async def save_flag(
        self,
        flag: Any,
        actor: str = "system",
        platform: str = "",
        action: str | None = None,
        audit: bool = True,
    ) -> None:
        """Persist *flag* to the backend and update the provider cache."""
        existing = await self.get_flag(flag.key)
        default_action = "flag_created" if existing is None else "flag_updated"
        await self.backend.save_flag(flag)
        if self._flag_provider is not None:
            self._flag_provider.upsert_flag(flag)
        if audit:
            await self._audit_rl(
                path=f"flag:{flag.key}",
                action=action or default_action,
                actor=actor,
                platform=platform,
            )

    async def delete_flag(
        self,
        key: str,
        actor: str = "system",
        platform: str = "",
        audit: bool = True,
    ) -> None:
        """Delete a flag by *key* from the backend and provider cache."""
        await self.backend.delete_flag(key)
        if self._flag_provider is not None:
            self._flag_provider.delete_flag(key)
        if audit:
            await self._audit_rl(
                path=f"flag:{key}",
                action="flag_deleted",
                actor=actor,
                platform=platform,
            )

    async def list_segments(self) -> list[Any]:
        """Return all segments from the provider cache (or backend)."""
        if self._flag_provider is not None:
            return list(self._flag_provider._segments.values())
        return await self.backend.load_all_segments()

    async def get_segment(self, key: str) -> Any:
        """Return a single ``Segment`` by *key*, or ``None`` if not found."""
        if self._flag_provider is not None:
            return self._flag_provider._segments.get(key)
        segments = await self.backend.load_all_segments()
        return next((s for s in segments if s.key == key), None)

    async def save_segment(
        self,
        segment: Any,
        actor: str = "system",
        platform: str = "",
        audit: bool = True,
    ) -> None:
        """Persist *segment* to the backend and update the provider cache."""
        existing = await self.get_segment(segment.key)
        action = "segment_created" if existing is None else "segment_updated"
        await self.backend.save_segment(segment)
        if self._flag_provider is not None:
            self._flag_provider.upsert_segment(segment)
        if audit:
            await self._audit_rl(
                path=f"segment:{segment.key}",
                action=action,
                actor=actor,
                platform=platform,
            )

    async def delete_segment(
        self,
        key: str,
        actor: str = "system",
        platform: str = "",
        audit: bool = True,
    ) -> None:
        """Delete a segment by *key* from the backend and provider cache."""
        await self.backend.delete_segment(key)
        if self._flag_provider is not None:
            self._flag_provider.delete_segment(key)
        if audit:
            await self._audit_rl(
                path=f"segment:{key}",
                action="segment_deleted",
                actor=actor,
                platform=platform,
            )

    async def _run_global_config_listener(self) -> None:
        """Background coroutine: invalidate the global config cache on remote changes.

        Iterates ``backend.subscribe_global_config()``.  Each ``None``
        yielded by the backend means another instance has written a new
        ``GlobalMaintenanceConfig`` to the shared store, so we drop the
        in-process cache and bump ``_schema_version`` so the OpenAPI schema
        cache is also rebuilt on the next ``/openapi.json`` request.

        If the backend raises ``NotImplementedError`` (MemoryBackend,
        FileBackend) the generator exits immediately and the task ends
        silently — no distributed invalidation, cache behaves as before.
        """
        try:
            async for _ in self.backend.subscribe_global_config():
                self._invalidate_global_config_cache()
                self._bump_schema_version()
                logger.debug("waygate: global config cache invalidated by remote change")
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — single-instance cache is fine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "waygate: global config listener crashed — invalidating cache as a precaution"
            )
            self._invalidate_global_config_cache()
            self._bump_schema_version()

    async def _run_route_state_listener(self) -> None:
        """Background coroutine: bump schema version on remote route state changes.

        Iterates ``backend.subscribe()``.  Each ``RouteState`` yielded
        means another worker has changed a route's lifecycle state
        (enable, disable, maintenance, etc.).  Bumping ``_schema_version``
        here ensures this worker's OpenAPI schema cache is invalidated so
        the next ``/openapi.json`` request reflects the current state read
        fresh from the backend — even if the change was made by a
        different Gunicorn worker.

        If the backend raises ``NotImplementedError`` (MemoryBackend,
        FileBackend) the generator exits immediately and the task ends
        silently.
        """
        try:
            async for _ in self.backend.subscribe():
                self._bump_schema_version()
                logger.debug("waygate: schema version bumped by remote route state change")
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — single-instance, no sync needed
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "waygate: route state listener crashed — bumping schema version as a precaution"
            )
            self._bump_schema_version()

    async def _run_rl_policy_listener(self) -> None:
        """Background coroutine: sync rate limit policy changes from other instances.

        Iterates ``backend.subscribe_rate_limit_policy()``.  Each event
        is either a ``"set"`` (apply the new policy to the local dict) or
        a ``"delete"`` (remove the key from the local dict).

        This keeps every worker's ``_rate_limit_policies`` in sync without
        a Redis round-trip on every request.

        If the backend raises ``NotImplementedError`` (MemoryBackend,
        FileBackend) the generator exits immediately and the task ends
        silently — single-instance deployments need no cross-process sync.
        """
        try:
            async for event in self.backend.subscribe_rate_limit_policy():
                action = event.get("action")
                key = event.get("key")
                if not key:
                    continue
                if action == "set":
                    policy_data = event.get("policy")
                    if policy_data:
                        try:
                            from waygate.core.rate_limit.models import RateLimitPolicy

                            policy = RateLimitPolicy.model_validate(policy_data)
                            # register_rate_limit initialises the limiter if needed.
                            await self.register_rate_limit(
                                path=policy.path, method=policy.method, policy=policy
                            )
                            logger.debug("waygate: rl policy synced from remote change: %s", key)
                        except Exception:
                            logger.exception(
                                "waygate: failed to apply remote rl policy change for %r", key
                            )
                elif action == "delete":
                    self._rate_limit_policies.pop(key, None)
                    logger.debug("waygate: rl policy deleted by remote change: %s", key)
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — per-process dict is the only store
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("waygate: rl policy listener crashed")

    # ------------------------------------------------------------------
    # Hot-path: check
    # ------------------------------------------------------------------

    async def check(
        self,
        path: str,
        method: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Evaluate the lifecycle policy for *path* (and optionally *method*).

        This is the single chokepoint for every incoming request.
        It raises a ``WaygateException`` subclass when the request should
        be blocked, and returns ``None`` when the request may proceed.

        Resolution order (highest priority first):
        1. Global maintenance (if enabled and path not in ``exempt_paths``)
        2. Method-specific state:  ``"GET:/payments"``
        3. Path-level state:       ``"/payments"``  (all methods)
        4. No state found → ACTIVE (fail-open)

        Fail-open: if the backend is unreachable, the error is logged and
        the request is allowed through — waygate never takes down an API.

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
            logger.exception("waygate: backend error reading global config — failing open")

        # 2. Per-route state check.
        state = await self._resolve_state(path, method)
        if state is None:
            return  # no state registered → effectively ACTIVE

        service = state.service if state is not None else None

        if state.status == RouteStatus.ACTIVE:
            return await self._run_rate_limit_check(path, method or "", context, service=service)

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
            # Rate limit check still runs for deprecated routes.
            return await self._run_rate_limit_check(path, method or "", context, service=service)

        # Rate limiting runs after all lifecycle checks (maintenance / disabled
        # routes short-circuit before touching any counters).  Within the rate
        # limit check, the global limit is evaluated first.
        await self._run_rate_limit_check(path, method or "", context, service=service)

    async def _run_rate_limit_check(
        self,
        path: str,
        method: str,
        context: dict[str, Any] | None,
        service: str | None = None,
    ) -> Any:
        """Run the rate limit check for *path*/*method* if a policy is registered.

        Applies the global rate limit first (higher precedence), then the
        per-route policy.  A request blocked by the global limit never
        touches the per-route counter.

        Returns the ``RateLimitResult`` (or ``None`` when no policy applies).
        Raises ``RateLimitExceededException`` when the limit is exceeded.
        """
        request = (context or {}).get("request")

        # Global rate limit takes precedence — checked first, same model as
        # global maintenance.  If the global limit is exceeded the per-route
        # check never runs and the per-route counter is not touched.
        grl = self._global_rate_limit_policy
        if grl is not None and grl.enabled:
            if not self._is_globally_exempt(path, method, grl.exempt_routes):
                await self._run_global_rate_limit_check(path, method, request, grl)

        # Per-service rate limit — runs after the all-services global limit
        # but before the per-route limit.  Only applies when the route belongs
        # to a named service and a policy has been configured for that service.
        if service:
            srl = self._service_rate_limit_policies.get(service)
            if srl is not None and srl.enabled:
                if not self._is_globally_exempt(path, method, srl.exempt_routes):
                    await self._run_service_rate_limit_check(path, method, request, srl, service)

        # Per-route check — only reached when the global limit passed (or the
        # route is exempt from the global limit, or no global limit is set).
        route_result: Any = None
        if self._rate_limiter is not None:
            policy_key = f"{method.upper()}:{path}" if method else f"ALL:{path}"
            policy = self._rate_limit_policies.get(policy_key)
            if policy is not None:
                custom_key_func = getattr(policy, "_custom_key_func", None)
                result = await self._rate_limiter.check(
                    path=path,
                    method=method or "GET",
                    request=request,
                    policy=policy,
                    custom_key_func=custom_key_func,
                )
                if not result.allowed:
                    await self._record_rate_limit_hit(path, method or "GET", policy, result)
                    raise RateLimitExceededException(
                        limit=result.limit,
                        retry_after_seconds=result.retry_after_seconds,
                        reset_at=result.reset_at,
                        remaining=0,
                        key=result.key,
                    )
                route_result = result

        return route_result

    async def _run_service_rate_limit_check(
        self,
        path: str,
        method: str,
        request: Any,
        srl_policy: Any,
        service: str,
    ) -> None:
        """Check the per-service rate limit for *path*/*method*.

        Uses ``__svc_rl:{service}__`` as the virtual path so all routes of
        the same service share one counter namespace.  Raises
        ``RateLimitExceededException`` when the limit is exceeded.
        """
        await self._ensure_rate_limiter()

        from waygate.core.rate_limit.models import RateLimitPolicy

        virtual_path = f"__svc_rl:{service}__"
        srl_as_policy = RateLimitPolicy(
            path=virtual_path,
            method="ALL",
            limit=srl_policy.limit,
            algorithm=srl_policy.algorithm,
            key_strategy=srl_policy.key_strategy,
            on_missing_key=srl_policy.on_missing_key,
            burst=srl_policy.burst,
        )

        result = await self._rate_limiter.check(
            path=virtual_path,
            method="ALL",
            request=request,
            policy=srl_as_policy,
        )

        if not result.allowed:
            await self._record_rate_limit_hit(path, method or "ALL", srl_as_policy, result)
            raise RateLimitExceededException(
                limit=result.limit,
                retry_after_seconds=result.retry_after_seconds,
                reset_at=result.reset_at,
                remaining=0,
                key=result.key,
            )

    def _is_globally_exempt(self, path: str, method: str, exempt_routes: list[str]) -> bool:
        """Return ``True`` if *path*/*method* is in the global exempt list.

        Each entry in *exempt_routes* is either:
        * a bare path (``"/health"``) — exempts all methods, or
        * a method-prefixed path (``"GET:/api/internal"``) — exempts that
          specific method only.
        """
        upper_method = method.upper() if method else ""
        for entry in exempt_routes:
            if ":" in entry and not entry.startswith("/"):
                em, _, ep = entry.partition(":")
                if em.upper() == upper_method and ep == path:
                    return True
            else:
                if entry == path:
                    return True
        return False

    async def _run_global_rate_limit_check(
        self,
        path: str,
        method: str,
        request: Any,
        grl_policy: Any,
    ) -> None:
        """Check the global rate limit for *path*/*method*.

        Uses ``__global__`` as the virtual path so all routes share the
        same counter namespace.  Raises ``RateLimitExceededException``
        when the limit is exceeded.
        """
        # Ensure the rate limiter is initialised without polluting
        # _rate_limit_policies with a fake "ALL:__global__" route entry.
        await self._ensure_rate_limiter()

        from waygate.core.rate_limit.models import RateLimitPolicy

        grl_as_policy = RateLimitPolicy(
            path="__global__",
            method="ALL",
            limit=grl_policy.limit,
            algorithm=grl_policy.algorithm,
            key_strategy=grl_policy.key_strategy,
            on_missing_key=grl_policy.on_missing_key,
            burst=grl_policy.burst,
        )

        result = await self._rate_limiter.check(
            path="__global__",
            method="ALL",
            request=request,
            policy=grl_as_policy,
        )

        if not result.allowed:
            await self._record_rate_limit_hit(path, method or "ALL", grl_as_policy, result)
            raise RateLimitExceededException(
                limit=result.limit,
                retry_after_seconds=result.retry_after_seconds,
                reset_at=result.reset_at,
                remaining=0,
                key=result.key,
            )

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
                logger.exception("waygate: backend error reading state for %r — failing open", key)
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
    # Registration (called by WaygateRouter at startup)
    # ------------------------------------------------------------------

    async def register(self, path: str, meta: dict[str, Any]) -> None:
        """Register a route from its ``__waygate_meta__`` decorator stamp.

        Called by ``WaygateRouter`` at application startup for every route
        that carries waygate metadata.

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
            ``WaygateRouter._waygate_routes``.
        """
        if not routes:
            return

        # One backend call to discover every already-persisted route.
        # Use get_registered_paths() instead of list_states() so that
        # backends that store routes under transformed keys (e.g.
        # WaygateServerBackend which adds a service prefix) correctly
        # compare against plain local paths.
        try:
            existing_keys: set[str] = await self.backend.get_registered_paths()
        except Exception:
            logger.exception(
                "waygate: register_batch — failed to list existing states, "
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

        # Restore persisted rate limit policies so CLI-set policies override
        # decorator-registered ones, and so policies survive restarts.
        await self.restore_rate_limit_policies()

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

        # Bare path not found — probe each HTTP method individually instead of
        # fetching all routes.  This avoids loading the entire route table
        # (SMEMBERS + MGET for Redis) just to find one or two method variants.
        if ":" not in path:
            matches: list[RouteState] = []
            for _m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                try:
                    matches.append(await self.backend.get_state(f"{_m}:{path}"))
                except (KeyError, Exception):
                    pass

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
            on the route state so the reason is visible in ``waygate status``.
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
        # Reset rate limit counters so clients are not penalised for retrying
        # during a maintenance window that has now ended.
        # reset(method=None) calls reset_all_for_path() — one operation instead
        # of seven sequential per-method calls.
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.reset(path=actual_path)
            except Exception:
                pass  # fail-open — counter reset is best-effort
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
    # Per-service global maintenance
    # ------------------------------------------------------------------

    @staticmethod
    def _service_global_key(service: str) -> str:
        return f"__waygate:svc_global:{service}__"

    async def get_service_maintenance(self, service: str) -> GlobalMaintenanceConfig:
        """Return the current per-service maintenance config for *service*."""
        key = self._service_global_key(service)
        try:
            state = await self.backend.get_state(key)
            return GlobalMaintenanceConfig.model_validate_json(state.reason)
        except (KeyError, Exception):
            return GlobalMaintenanceConfig()

    async def enable_service_maintenance(
        self,
        service: str,
        reason: str = "",
        exempt_paths: list[str] | None = None,
        include_force_active: bool = False,
        actor: str = "system",
        platform: str = "system",
    ) -> GlobalMaintenanceConfig:
        """Enable maintenance mode for all routes belonging to *service*.

        Stores a per-service sentinel in the backend (similar to the
        all-services global maintenance sentinel).  SDK clients with the
        matching ``app_id`` pick this up via SSE and apply it as an
        effective global maintenance for their service only.
        """
        key = self._service_global_key(service)
        cfg = GlobalMaintenanceConfig(
            enabled=True,
            reason=reason,
            exempt_paths=exempt_paths or [],
            include_force_active=include_force_active,
        )
        sentinel = RouteState(
            path=key,
            status=RouteStatus.ACTIVE,
            reason=cfg.model_dump_json(),
            service=service,
        )
        await self.backend.set_state(key, sentinel)
        await self._audit(
            path=key,
            action="service_maintenance_on",
            actor=actor,
            reason=reason,
            platform=platform,
            previous_status=RouteStatus.ACTIVE,
            new_status=RouteStatus.MAINTENANCE,
        )
        return cfg

    async def disable_service_maintenance(
        self,
        service: str,
        actor: str = "system",
        platform: str = "system",
    ) -> GlobalMaintenanceConfig:
        """Disable per-service maintenance, restoring normal per-route state."""
        key = self._service_global_key(service)
        cfg = GlobalMaintenanceConfig(enabled=False)
        sentinel = RouteState(
            path=key,
            status=RouteStatus.ACTIVE,
            reason=cfg.model_dump_json(),
            service=service,
        )
        await self.backend.set_state(key, sentinel)
        await self._audit(
            path=key,
            action="service_maintenance_off",
            actor=actor,
            platform=platform,
            previous_status=RouteStatus.MAINTENANCE,
            new_status=RouteStatus.ACTIVE,
        )
        return cfg

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
        """Schedule a single fire-and-forget dispatch task for all webhooks.

        Deduplication is handled inside ``_dispatch_webhooks``: the backend
        is asked to claim exclusive dispatch rights via a deterministic key
        before any HTTP POSTs are sent.  On single-instance backends
        (MemoryBackend, FileBackend) the claim always succeeds.  On
        RedisBackend only the first instance to win ``SET NX`` fires;
        all others silently skip.

        Failures are logged and never propagated — a broken webhook must
        never affect the hot path.
        """
        if not self._webhooks:
            return
        asyncio.create_task(
            self._dispatch_webhooks(event, path, state),
            name=f"waygate-webhook:{event}:{path}",
        )

    async def _dispatch_webhooks(self, event: str, path: str, state: RouteState) -> None:
        """Claim dispatch rights then POST to every registered webhook URL.

        Uses ``backend.try_claim_webhook_dispatch()`` with a deterministic
        dedup key derived from ``event``, ``path``, and the full serialised
        ``RouteState``.  Because the scheduler produces identical
        ``RouteState`` objects on all instances for the same window
        activation, the dedup key is the same across the entire fleet and
        only one instance wins the ``SET NX`` claim.
        """
        raw = f"{event}:{path}:{state.model_dump_json()}"
        dedup_key = hashlib.sha256(raw.encode()).hexdigest()
        try:
            claimed = await self.backend.try_claim_webhook_dispatch(dedup_key)
        except Exception:
            logger.warning("waygate: webhook dedup check raised unexpectedly — firing anyway")
            claimed = True  # fail-open: over-deliver rather than miss

        if not claimed:
            logger.debug(
                "waygate: webhook dispatch for %r/%s already claimed by another instance"
                " — skipping",
                event,
                path,
            )
            return

        for url, formatter in self._webhooks:
            payload = formatter(event, path, state)
            asyncio.create_task(
                self._post_webhook(url, payload),
                name=f"waygate-webhook-post:{event}:{path}",
            )

    @staticmethod
    async def _post_webhook(url: str, payload: dict[str, Any]) -> None:
        """Send a single webhook POST.  Errors are logged, not raised."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json=payload)
        except Exception:
            logger.exception("waygate: webhook POST to %r failed", url)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    async def _ensure_rate_limiter(self) -> None:
        """Lazily initialise the rate limiter without registering any policy.

        Safe to call multiple times — no-op once the limiter exists.
        """
        if self._rate_limiter is not None:
            return
        try:
            from waygate.core.rate_limit.limiter import WaygateRateLimiter
            from waygate.core.rate_limit.storage import create_rate_limit_storage

            storage = create_rate_limit_storage(
                self.backend,
                self._rate_limit_backend,
                snapshot_interval_seconds=self._rate_limit_snapshot_interval,
            )
            from waygate.core.rate_limit.models import RateLimitAlgorithm

            algo = self._default_rate_limit_algorithm or RateLimitAlgorithm.FIXED_WINDOW
            self._rate_limiter = WaygateRateLimiter(
                storage=storage,
                default_algorithm=algo,
            )
            # Kick off storage background tasks (e.g. FileRateLimitStorage
            # snapshot writer) now, in async context, so they are running
            # before the first request rather than lazily per-increment.
            await self._rate_limiter.startup()
        except ImportError:
            raise ImportError(
                "Rate limiting requires the 'limits' library. "
                "Install it with: pip install waygate[rate-limit]"
            ) from None

    async def register_rate_limit(
        self,
        path: str,
        method: str,
        policy: Any,
    ) -> None:
        """Register a rate limit policy for *path*/*method*.

        Lazily initialises the rate limiter on the first call so that
        importing ``waygate`` without the ``limits`` library installed works
        fine — the ``ImportError`` is only raised when rate limiting is
        actually configured.

        Parameters
        ----------
        path:
            Route path template (e.g. ``"/api/payments"``).
        method:
            HTTP method (``"GET"``, ``"POST"``, …) or ``"ALL"`` for any method.
        policy:
            A ``RateLimitPolicy`` instance describing the limit.
        """
        await self._ensure_rate_limiter()
        key = f"{method.upper()}:{path}"
        self._rate_limit_policies[key] = policy

    async def _record_rate_limit_hit(
        self,
        path: str,
        method: str,
        policy: Any,
        result: Any,
    ) -> None:
        """Write a ``RateLimitHit`` record to the backend.

        Errors are caught and logged — a failure to record a hit must never
        affect the 429 response already being built.
        """
        try:
            from waygate.core.rate_limit.models import RateLimitHit

            now = datetime.now(UTC)
            hit = RateLimitHit(
                id=str(uuid.uuid4()),
                timestamp=now,
                path=path,
                method=method,
                key=result.key,
                limit=result.limit,
                tier=result.tier,
                reset_at=result.reset_at,
            )
            await self.backend.write_rate_limit_hit(hit)
        except Exception:
            logger.exception("waygate: failed to record rate limit hit for %r", path)

    async def get_rate_limit_hits(
        self,
        path: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """Return recent rate limit hits from the backend."""
        return await self.backend.get_rate_limit_hits(path=path, limit=limit)

    async def reset_rate_limit(
        self,
        path: str,
        method: str | None = None,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Reset rate limit counters for *path*.

        Parameters
        ----------
        path:
            Route path template.
        method:
            When provided, only resets counters for this method.
            When ``None``, resets all methods.
        """
        if self._rate_limiter is None:
            return
        await self._rate_limiter.reset(path=path, method=method)
        composite = f"{method.upper()}:{path}" if method else path
        await self._audit_rl(
            path=composite,
            action="rl_reset",
            actor=actor,
            platform=platform,
        )

    @staticmethod
    def _validate_limit_string(limit: str) -> None:
        """Raise ``ValueError`` with a helpful message if *limit* is not a valid rate limit string.

        Uses the ``limits`` library's own parser so the check is authoritative.
        Only called when the library is installed; silently skips otherwise.
        """
        try:
            from limits import parse as _parse
        except ImportError:
            return  # limits not installed yet; engine._ensure_rate_limiter will handle it
        try:
            _parse(limit)
        except ValueError:
            valid = "second, minute, hour, day, month, year (or their plurals)"
            raise ValueError(
                f"Invalid rate limit string {limit!r}. "
                f"Use the format '<count>/<granularity>', e.g. '100/minute'. "
                f"Valid granularities: {valid}."
            ) from None

    async def set_rate_limit_policy(
        self,
        path: str,
        method: str,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        burst: int = 0,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Persist a rate limit policy for *path*/*method* and register it live.

        Persists to the backend so the policy survives restarts and is
        visible to other instances.  Also registers the policy in-process
        so it takes effect immediately without a restart.

        Returns the ``RateLimitPolicy`` instance.

        Raises
        ------
        ValueError
            When *limit* is not a valid rate limit string (e.g. ``"100/minutesedrr"``).
        RouteNotFoundException
            When *path* is not registered in the backend.  Rate limit policies
            are only meaningful for routes that actually exist — applying one to
            an unknown path would create a phantom entry that never fires.
        """
        self._validate_limit_string(limit)
        # Guard: verify the route is registered before creating a policy.
        # AmbiguousRouteError means the path exists under several HTTP methods,
        # which is perfectly valid for a per-path rate limit.
        try:
            await self._resolve_existing(path)
        except AmbiguousRouteError:
            pass  # route exists — just registered under multiple methods

        from waygate.core.rate_limit.models import (
            RateLimitAlgorithm,
            RateLimitKeyStrategy,
            RateLimitPolicy,
        )

        algo = RateLimitAlgorithm(algorithm) if algorithm else RateLimitAlgorithm.FIXED_WINDOW
        key_strat = RateLimitKeyStrategy(key_strategy) if key_strategy else RateLimitKeyStrategy.IP
        composite = f"{method.upper()}:{path}"
        is_update = composite in self._rate_limit_policies
        policy = RateLimitPolicy(
            path=path,
            method=method.upper(),
            limit=limit,
            algorithm=algo,
            key_strategy=key_strat,
            burst=burst,
        )
        # Register live in the rate limiter (initialises it if needed).
        await self.register_rate_limit(path=path, method=method.upper(), policy=policy)
        # Persist so other instances + restarts pick it up.
        await self.backend.set_rate_limit_policy(path, method, policy.model_dump(mode="json"))
        logger.info(
            "waygate: rate limit policy %s for %s %s (%s) by %s",
            "updated" if is_update else "set",
            method,
            path,
            limit,
            actor,
        )
        await self._audit_rl(
            path=composite,
            action="rl_policy_updated" if is_update else "rl_policy_set",
            actor=actor,
            reason=f"{limit} · {algo} · {key_strat}",
            platform=platform,
        )
        return policy

    async def delete_rate_limit_policy(
        self,
        path: str,
        method: str,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Remove a rate limit policy for *path*/*method*.

        Removes from the in-process registry and from the backend so the
        removal is permanent across restarts.
        """
        key = f"{method.upper()}:{path}"
        self._rate_limit_policies.pop(key, None)
        await self.backend.delete_rate_limit_policy(path, method)
        logger.info("waygate: rate limit policy deleted for %s %s by %s", method, path, actor)
        await self._audit_rl(
            path=key,
            action="rl_policy_deleted",
            actor=actor,
            platform=platform,
        )

    async def restore_rate_limit_policies(self) -> None:
        """Load persisted rate limit policies from the backend into memory.

        Called at the end of ``register_batch()`` so that CLI-set policies
        (which are persisted) override decorator-registered ones.  Also
        useful for re-hydrating the in-process registry after a restart.
        """
        try:
            policy_dicts = await self.backend.get_rate_limit_policies()
        except Exception:
            logger.exception("waygate: failed to restore rate limit policies from backend")
            return

        if not policy_dicts:
            return

        for policy_data in policy_dicts:
            try:
                from waygate.core.rate_limit.models import RateLimitPolicy

                policy = RateLimitPolicy.model_validate(policy_data)
                await self.register_rate_limit(
                    path=policy.path, method=policy.method, policy=policy
                )
            except Exception:
                logger.exception("waygate: failed to restore rate limit policy %r", policy_data)

        # Also restore the global rate limit policy if one was persisted.
        await self._restore_global_rate_limit_policy()
        await self._restore_service_rate_limit_policies()

    async def _restore_global_rate_limit_policy(self) -> None:
        """Load the persisted global rate limit policy from the backend."""
        try:
            policy_data = await self.backend.get_global_rate_limit_policy()
        except Exception:
            logger.exception("waygate: failed to restore global rate limit policy from backend")
            return

        if not policy_data:
            return

        try:
            from waygate.core.rate_limit.models import GlobalRateLimitPolicy

            self._global_rate_limit_policy = GlobalRateLimitPolicy.model_validate(policy_data)
            logger.info(
                "waygate: restored global rate limit policy (%s)",
                self._global_rate_limit_policy.limit,
            )
        except Exception:
            logger.exception("waygate: failed to parse persisted global rate limit policy")

    async def set_global_rate_limit(
        self,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        on_missing_key: str | None = None,
        burst: int = 0,
        exempt_routes: list[str] | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Set or update the global rate limit policy.

        The global policy applies to every route that is not listed in
        *exempt_routes*.  Persisted so the policy survives restarts.

        Returns the ``GlobalRateLimitPolicy`` instance.
        """
        self._validate_limit_string(limit)
        from waygate.core.rate_limit.models import (
            GlobalRateLimitPolicy,
            OnMissingKey,
            RateLimitAlgorithm,
            RateLimitKeyStrategy,
        )

        algo = RateLimitAlgorithm(algorithm) if algorithm else RateLimitAlgorithm.FIXED_WINDOW
        key_strat = RateLimitKeyStrategy(key_strategy) if key_strategy else RateLimitKeyStrategy.IP
        omk = OnMissingKey(on_missing_key) if on_missing_key else None

        is_update = self._global_rate_limit_policy is not None
        policy = GlobalRateLimitPolicy(
            limit=limit,
            algorithm=algo,
            key_strategy=key_strat,
            on_missing_key=omk,
            burst=burst,
            exempt_routes=exempt_routes or [],
            enabled=True,
        )
        self._global_rate_limit_policy = policy
        await self.backend.set_global_rate_limit_policy(policy.model_dump(mode="json"))
        logger.info(
            "waygate: global rate limit policy %s (%s) by %s",
            "updated" if is_update else "set",
            limit,
            actor,
        )
        await self._audit_rl(
            path="__global_rl__",
            action="global_rl_updated" if is_update else "global_rl_set",
            actor=actor,
            reason=f"{limit} · {algo} · {key_strat}",
            platform=platform,
        )
        return policy

    async def get_global_rate_limit(self) -> Any:
        """Return the current ``GlobalRateLimitPolicy``, or ``None``."""
        return self._global_rate_limit_policy

    async def delete_global_rate_limit(
        self,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Remove the global rate limit policy.

        Clears the in-process policy and removes the persisted entry.
        """
        self._global_rate_limit_policy = None
        await self.backend.delete_global_rate_limit_policy()
        logger.info("waygate: global rate limit policy deleted by %s", actor)
        await self._audit_rl(
            path="__global_rl__",
            action="global_rl_deleted",
            actor=actor,
            platform=platform,
        )

    async def reset_global_rate_limit(
        self,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Reset global rate limit counters.

        Clears the ``__global__`` counter namespace so the rate limit
        starts fresh.  The policy itself is not removed.
        """
        if self._rate_limiter is None:
            return
        await self._rate_limiter.reset(path="__global__", method="ALL")
        await self._audit_rl(
            path="__global_rl__",
            action="global_rl_reset",
            actor=actor,
            platform=platform,
        )

    async def enable_global_rate_limit(
        self,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Re-enable a paused global rate limit policy."""
        if self._global_rate_limit_policy is None or self._global_rate_limit_policy.enabled:
            return
        self._global_rate_limit_policy = self._global_rate_limit_policy.model_copy(
            update={"enabled": True}
        )
        await self.backend.set_global_rate_limit_policy(
            self._global_rate_limit_policy.model_dump(mode="json")
        )
        await self._audit_rl(
            path="__global_rl__",
            action="global_rl_enabled",
            actor=actor,
            platform=platform,
        )

    async def disable_global_rate_limit(
        self,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Pause (disable) the global rate limit policy without removing it."""
        if self._global_rate_limit_policy is None or not self._global_rate_limit_policy.enabled:
            return
        self._global_rate_limit_policy = self._global_rate_limit_policy.model_copy(
            update={"enabled": False}
        )
        await self.backend.set_global_rate_limit_policy(
            self._global_rate_limit_policy.model_dump(mode="json")
        )
        await self._audit_rl(
            path="__global_rl__",
            action="global_rl_disabled",
            actor=actor,
            platform=platform,
        )

    async def _restore_service_rate_limit_policies(self) -> None:
        """Load all persisted per-service rate limit policies from the backend."""
        try:
            all_policies = await self.backend.get_all_service_rate_limit_policies()
        except Exception:
            logger.exception("waygate: failed to restore service rate limit policies from backend")
            return

        from waygate.core.rate_limit.models import GlobalRateLimitPolicy

        for service, policy_data in all_policies.items():
            try:
                self._service_rate_limit_policies[service] = GlobalRateLimitPolicy.model_validate(
                    policy_data
                )
                logger.info("waygate: restored service rate limit policy for %r", service)
            except Exception:
                logger.exception(
                    "waygate: failed to parse service rate limit policy for %r", service
                )

    # ------------------------------------------------------------------
    # Per-service rate limit CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _service_rl_key(service: str) -> str:
        return f"__waygate:svc_rl:{service}__"

    async def get_service_rate_limit(self, service: str) -> Any:
        """Return the current ``GlobalRateLimitPolicy`` for *service*, or ``None``."""
        return self._service_rate_limit_policies.get(service)

    async def set_service_rate_limit(
        self,
        service: str,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        on_missing_key: str | None = None,
        burst: int = 0,
        exempt_routes: list[str] | None = None,
        actor: str = "system",
        platform: str = "system",
    ) -> Any:
        """Set or update the per-service rate limit policy for *service*.

        Applies to all routes of *service* combined.  Persisted so the
        policy survives restarts.  Returns the ``GlobalRateLimitPolicy``.
        """
        self._validate_limit_string(limit)
        from waygate.core.rate_limit.models import (
            GlobalRateLimitPolicy,
            OnMissingKey,
            RateLimitAlgorithm,
            RateLimitKeyStrategy,
        )

        algo = RateLimitAlgorithm(algorithm) if algorithm else RateLimitAlgorithm.FIXED_WINDOW
        key_strat = RateLimitKeyStrategy(key_strategy) if key_strategy else RateLimitKeyStrategy.IP
        omk = OnMissingKey(on_missing_key) if on_missing_key else None

        is_update = service in self._service_rate_limit_policies
        policy = GlobalRateLimitPolicy(
            limit=limit,
            algorithm=algo,
            key_strategy=key_strat,
            on_missing_key=omk,
            burst=burst,
            exempt_routes=exempt_routes or [],
            enabled=True,
        )
        self._service_rate_limit_policies[service] = policy
        await self.backend.set_service_rate_limit_policy(service, policy.model_dump(mode="json"))
        logger.info(
            "waygate: service rate limit policy %s for %r (%s) by %s",
            "updated" if is_update else "set",
            service,
            limit,
            actor,
        )
        await self._audit_rl(
            path=self._service_rl_key(service),
            action="svc_rl_updated" if is_update else "svc_rl_set",
            actor=actor,
            reason=f"{limit} · {algo} · {key_strat}",
            platform=platform,
        )
        return policy

    async def delete_service_rate_limit(
        self,
        service: str,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Remove the per-service rate limit policy for *service*."""
        self._service_rate_limit_policies.pop(service, None)
        await self.backend.delete_service_rate_limit_policy(service)
        logger.info("waygate: service rate limit policy deleted for %r by %s", service, actor)
        await self._audit_rl(
            path=self._service_rl_key(service),
            action="svc_rl_deleted",
            actor=actor,
            platform=platform,
        )

    async def reset_service_rate_limit(
        self,
        service: str,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Reset the in-process rate limit counters for *service*.

        Clears the ``__svc_rl:{service}__`` counter namespace.  The policy
        itself is not removed.
        """
        if self._rate_limiter is None:
            return
        virtual_path = f"__svc_rl:{service}__"
        await self._rate_limiter.reset(path=virtual_path, method="ALL")
        await self._audit_rl(
            path=self._service_rl_key(service),
            action="svc_rl_reset",
            actor=actor,
            platform=platform,
        )

    async def enable_service_rate_limit(
        self,
        service: str,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Re-enable a paused per-service rate limit policy."""
        policy = self._service_rate_limit_policies.get(service)
        if policy is None or policy.enabled:
            return
        self._service_rate_limit_policies[service] = policy.model_copy(update={"enabled": True})
        await self.backend.set_service_rate_limit_policy(
            service, self._service_rate_limit_policies[service].model_dump(mode="json")
        )
        await self._audit_rl(
            path=self._service_rl_key(service),
            action="svc_rl_enabled",
            actor=actor,
            platform=platform,
        )

    async def disable_service_rate_limit(
        self,
        service: str,
        *,
        actor: str = "system",
        platform: str = "system",
    ) -> None:
        """Pause (disable) a per-service rate limit policy without removing it."""
        policy = self._service_rate_limit_policies.get(service)
        if policy is None or not policy.enabled:
            return
        self._service_rate_limit_policies[service] = policy.model_copy(update={"enabled": False})
        await self.backend.set_service_rate_limit_policy(
            service, self._service_rate_limit_policies[service].model_dump(mode="json")
        )
        await self._audit_rl(
            path=self._service_rl_key(service),
            action="svc_rl_disabled",
            actor=actor,
            platform=platform,
        )

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
        return [s for s in states if not s.path.startswith("__waygate:")]

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
        """Write an audit entry for a route state change."""
        # Carry the service label forward so audit rows can be filtered by service.
        service: str | None = None
        try:
            state = await self.backend.get_state(path)
            service = state.service
        except Exception:  # noqa: BLE001
            pass
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            path=path,
            service=service,
            action=action,
            actor=actor,
            platform=platform,
            reason=reason,
            previous_status=previous_status,
            new_status=new_status,
        )
        await self.backend.write_audit(entry)

    async def _audit_rl(
        self,
        path: str,
        action: str,
        actor: str = "system",
        reason: str = "",
        platform: str = "system",
    ) -> None:
        """Write an audit entry for a rate limit policy mutation."""
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            path=path,
            action=action,
            actor=actor,
            platform=platform,
            reason=reason,
        )
        await self.backend.write_audit(entry)
