"""ShieldEngine — the central orchestrator for api-shield.

All business logic lives here. Middleware and decorators are transport
layers that call into the engine. They never make state decisions themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
    RateLimitExceededException,
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
        rate_limit_backend: ShieldBackend | None = None,
        default_rate_limit_algorithm: Any = None,
        rate_limit_snapshot_interval: int = 10,
        max_rl_hit_entries: int = 10_000,
    ) -> None:
        self.backend: ShieldBackend = backend or MemoryBackend(
            max_rl_hit_entries=max_rl_hit_entries,
        )
        self.current_env = current_env
        # Scheduler is lazily imported to avoid a circular reference.
        from shield.core.scheduler import MaintenanceScheduler

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
        self._rate_limit_backend: ShieldBackend | None = rate_limit_backend
        self._rate_limit_snapshot_interval: int = rate_limit_snapshot_interval
        # Lazily set to a RateLimitAlgorithm enum value on first use.
        self._default_rate_limit_algorithm: Any = default_rate_limit_algorithm
        self._rate_limiter: Any = None  # ShieldRateLimiter | None
        self._rate_limit_policies: dict[str, Any] = {}  # "METHOD:/path" → RateLimitPolicy

    # ------------------------------------------------------------------
    # Async context manager — calls backend lifecycle hooks
    # ------------------------------------------------------------------

    async def __aenter__(self) -> ShieldEngine:
        """Call ``backend.startup()``, start background tasks, and return *self*.

        Use ``async with ShieldEngine(...) as engine:`` to ensure the
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

        * ``shield-global-config-listener`` — invalidates the in-process
          global maintenance config cache when another instance writes a
          new config, and bumps ``_schema_version`` so the OpenAPI schema
          cache is rebuilt on the next request.
        * ``shield-route-state-listener`` — bumps ``_schema_version``
          whenever another instance changes any route state (enable,
          disable, maintenance, etc.) so this worker's OpenAPI schema
          cache is invalidated and rebuilt with the current state.
        * ``shield-rl-policy-listener`` — syncs rate limit policy changes
          made on any instance into this instance's ``_rate_limit_policies``
          dict so every worker always enforces the current policy.

        All listeners exit silently when the backend raises
        ``NotImplementedError`` (``MemoryBackend``, ``FileBackend``).

        Safe to call multiple times — already-running tasks are left alone.
        """
        if self._global_listener_task is None or self._global_listener_task.done():
            self._global_listener_task = asyncio.create_task(
                self._run_global_config_listener(),
                name="shield-global-config-listener",
            )
        if self._route_state_listener_task is None or self._route_state_listener_task.done():
            self._route_state_listener_task = asyncio.create_task(
                self._run_route_state_listener(),
                name="shield-route-state-listener",
            )
        if self._rl_policy_listener_task is None or self._rl_policy_listener_task.done():
            self._rl_policy_listener_task = asyncio.create_task(
                self._run_rl_policy_listener(),
                name="shield-rl-policy-listener",
            )

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
                logger.debug("shield: global config cache invalidated by remote change")
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — single-instance cache is fine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "shield: global config listener crashed — invalidating cache as a precaution"
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
                logger.debug("shield: schema version bumped by remote route state change")
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — single-instance, no sync needed
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "shield: route state listener crashed — bumping schema version as a precaution"
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
                            from shield.core.rate_limit.models import RateLimitPolicy

                            policy = RateLimitPolicy.model_validate(policy_data)
                            # register_rate_limit initialises the limiter if needed.
                            await self.register_rate_limit(
                                path=policy.path, method=policy.method, policy=policy
                            )
                            logger.debug("shield: rl policy synced from remote change: %s", key)
                        except Exception:
                            logger.exception(
                                "shield: failed to apply remote rl policy change for %r", key
                            )
                elif action == "delete":
                    self._rate_limit_policies.pop(key, None)
                    logger.debug("shield: rl policy deleted by remote change: %s", key)
        except NotImplementedError:
            pass  # backend doesn't support pub/sub — per-process dict is the only store
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shield: rl policy listener crashed")

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
            return await self._run_rate_limit_check(path, method or "", context)

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
            return await self._run_rate_limit_check(path, method or "", context)

        # Rate limiting runs after all lifecycle checks so that maintenance /
        # disabled routes short-circuit before touching counters.
        await self._run_rate_limit_check(path, method or "", context)

    async def _run_rate_limit_check(
        self, path: str, method: str, context: dict[str, Any] | None
    ) -> Any:
        """Run the rate limit check for *path*/*method* if a policy is registered.

        Returns the ``RateLimitResult`` (or ``None`` when no policy applies).
        Raises ``RateLimitExceededException`` when the limit is exceeded.
        """
        if self._rate_limiter is None:
            return None

        policy_key = f"{method.upper()}:{path}" if method else f"ALL:{path}"
        policy = self._rate_limit_policies.get(policy_key)
        if policy is None:
            return None

        request = (context or {}).get("request")
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

        return result

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
            name=f"shield-webhook:{event}:{path}",
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
            logger.warning("shield: webhook dedup check raised unexpectedly — firing anyway")
            claimed = True  # fail-open: over-deliver rather than miss

        if not claimed:
            logger.debug(
                "shield: webhook dispatch for %r/%s already claimed by another instance — skipping",
                event,
                path,
            )
            return

        for url, formatter in self._webhooks:
            payload = formatter(event, path, state)
            asyncio.create_task(
                self._post_webhook(url, payload),
                name=f"shield-webhook-post:{event}:{path}",
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
    # Rate limiting
    # ------------------------------------------------------------------

    async def register_rate_limit(
        self,
        path: str,
        method: str,
        policy: Any,
    ) -> None:
        """Register a rate limit policy for *path*/*method*.

        Lazily initialises the rate limiter on the first call so that
        importing ``shield`` without the ``limits`` library installed works
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
        if self._rate_limiter is None:
            try:
                from shield.core.rate_limit.limiter import ShieldRateLimiter
                from shield.core.rate_limit.storage import create_rate_limit_storage

                storage = create_rate_limit_storage(
                    self.backend,
                    self._rate_limit_backend,
                    snapshot_interval_seconds=self._rate_limit_snapshot_interval,
                )
                from shield.core.rate_limit.models import RateLimitAlgorithm

                algo = self._default_rate_limit_algorithm or RateLimitAlgorithm.FIXED_WINDOW
                self._rate_limiter = ShieldRateLimiter(
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
                    "Install it with: pip install api-shield[rate-limit]"
                ) from None

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
            from shield.core.rate_limit.models import RateLimitHit

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
            logger.exception("shield: failed to record rate limit hit for %r", path)

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
        """
        from shield.core.rate_limit.models import (
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
            "shield: rate limit policy %s for %s %s (%s) by %s",
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
        logger.info("shield: rate limit policy deleted for %s %s by %s", method, path, actor)
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
            logger.exception("shield: failed to restore rate limit policies from backend")
            return

        if not policy_dicts:
            return

        for policy_data in policy_dicts:
            try:
                from shield.core.rate_limit.models import RateLimitPolicy

                policy = RateLimitPolicy.model_validate(policy_data)
                await self.register_rate_limit(
                    path=policy.path, method=policy.method, policy=policy
                )
            except Exception:
                logger.exception("shield: failed to restore rate limit policy %r", policy_data)

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
        """Write an audit entry for a route state change."""
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
