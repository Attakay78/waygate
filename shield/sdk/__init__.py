"""ShieldSDK — connect a FastAPI app to a remote Shield Server.

Drop-in alternative to the embedded setup.  State is managed centrally
from the Shield Server dashboard or CLI; this SDK enforces it locally on
every request with zero network overhead.

Usage::

    from shield.sdk import ShieldSDK

    sdk = ShieldSDK(
        server_url="http://shield-server:9000",
        app_id="payments-service",
        token="...",    # omit if server has no auth
    )
    sdk.attach(app)

    @app.get("/payments")
    @maintenance(reason="DB migration")   # optional — manage from dashboard instead
    async def payments():
        return {"ok": True}

The CLI then points at the Shield Server, not at this service::

    shield config set-url http://shield-server:9000
    shield status                         # routes from ALL connected services
    shield disable payments-service /api/payments --reason "migration"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from shield.core.backends.base import ShieldBackend
from shield.core.backends.server import ShieldServerBackend
from shield.core.engine import ShieldEngine

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["ShieldSDK"]

logger = logging.getLogger(__name__)


class ShieldSDK:
    """Connect a FastAPI application to a remote Shield Server.

    Parameters
    ----------
    server_url:
        Base URL of the Shield Server (e.g. ``http://shield-server:9000``).
        If the Shield Server is mounted under a prefix (e.g. ``/shield``),
        include the prefix: ``http://myapp.com/shield``.
    app_id:
        Unique name for this service shown in the Shield Server dashboard.
        Use a stable identifier like ``"payments-service"`` or
        ``"orders-api"``.
    token:
        Pre-issued bearer token for Shield Server auth.  Takes priority
        over ``username``/``password`` if both are provided.  Omit if
        the server has no auth configured.
    username:
        Shield Server username.  When provided alongside ``password``
        (and no ``token``), the SDK automatically calls
        ``POST /api/auth/login`` on startup with ``platform="sdk"`` and
        obtains a long-lived service token — no manual token management
        required.  Store credentials in environment variables and inject
        them at deploy time::

            sdk = ShieldSDK(
                server_url=os.environ["SHIELD_SERVER_URL"],
                app_id="payments-service",
                username=os.environ["SHIELD_USERNAME"],
                password=os.environ["SHIELD_PASSWORD"],
            )
    password:
        Shield Server password.  Used together with ``username``.
    reconnect_delay:
        Seconds between SSE reconnect attempts after a dropped connection.
        Defaults to 5 seconds.
    rate_limit_backend:
        Optional shared backend for rate limit counter storage.  When
        ``None`` (default) each instance maintains its own in-process
        counters — a ``100/minute`` limit is enforced independently on
        each replica.  Pass a :class:`~shield.core.backends.redis.RedisBackend`
        pointing at a shared Redis instance to enforce the limit
        **across all replicas combined**::

            from shield.core.backends.redis import RedisBackend

            sdk = ShieldSDK(
                server_url="http://shield:9000",
                app_id="payments-service",
                rate_limit_backend=RedisBackend(url="redis://redis:6379/1"),
            )
    """

    def __init__(
        self,
        server_url: str,
        app_id: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        reconnect_delay: float = 5.0,
        rate_limit_backend: ShieldBackend | None = None,
    ) -> None:
        self._backend = ShieldServerBackend(
            server_url=server_url,
            app_id=app_id,
            token=token,
            username=username,
            password=password,
            reconnect_delay=reconnect_delay,
        )
        self._engine = ShieldEngine(
            backend=self._backend,
            rate_limit_backend=rate_limit_backend,
        )

    @property
    def engine(self) -> ShieldEngine:
        """The underlying :class:`~shield.core.engine.ShieldEngine`.

        Use this if you need direct engine access (e.g. to call
        ``engine.disable()`` programmatically from within the service).
        """
        return self._engine

    def use_openfeature(
        self,
        hooks: list[Any] | None = None,
        domain: str = "shield",
    ) -> None:
        """Enable OpenFeature feature-flag evaluation for this SDK client.

        Must be called **before** :meth:`attach`.

        Activates :class:`~shield.sdk.flag_provider.ShieldSDKFlagProvider`
        which:

        * On startup fetches all flags/segments from the Shield Server via
          ``GET /api/flags`` and ``GET /api/segments``.
        * Stays current by listening to ``flag_updated``, ``flag_deleted``,
          ``segment_updated``, and ``segment_deleted`` events on the
          existing SSE connection — no extra network connections needed.

        Usage::

            sdk = ShieldSDK(server_url="http://shield:9000", app_id="my-svc")
            sdk.use_openfeature()
            sdk.attach(app)

            # Evaluate anywhere via the engine's flag client:
            value = await sdk.engine.flag_client.get_boolean_value(
                "my-flag", default_value=False
            )

        Parameters
        ----------
        hooks:
            Optional list of OpenFeature :class:`Hook` objects to register
            globally for this provider.
        domain:
            OpenFeature provider domain name (default ``"shield"``).
        """
        from shield.sdk.flag_provider import ShieldSDKFlagProvider

        provider = ShieldSDKFlagProvider(self._backend)
        self._engine.use_openfeature(provider=provider, hooks=hooks, domain=domain)

    def attach(self, app: FastAPI) -> None:
        """Wire shield middleware and lifecycle hooks into *app*.

        Call this once after creating the FastAPI app and before
        defining routes::

            sdk.attach(app)

            @app.get("/payments")
            async def payments():
                ...

        What ``attach`` does:

        1. Adds :class:`~shield.fastapi.middleware.ShieldMiddleware` so
           every request is checked against the local state cache.
        2. On startup: syncs state from the Shield Server, starts the SSE
           listener, discovers decorated routes, and registers any new
           ones with the server.
        3. On shutdown: closes the SSE connection and HTTP client cleanly.

        Parameters
        ----------
        app:
            The :class:`fastapi.FastAPI` application to attach to.
        """
        from fastapi.routing import APIRoute

        from shield.fastapi.middleware import ShieldMiddleware

        app.add_middleware(ShieldMiddleware, engine=self._engine)

        @app.on_event("startup")
        async def _shield_sdk_startup() -> None:
            # Start engine background tasks (pub/sub listeners, etc.)
            await self._engine.start()
            # Connect to Shield Server: sync state + open SSE stream.
            await self._backend.startup()

            # Discover routes decorated with @maintenance, @disabled, etc.
            # and register any that are new to the Shield Server.
            # Use the same method-prefixed key format as ShieldRouter
            # (e.g. "GET:/api/payments") so that routes registered by
            # ShieldRouter before the SDK startup don't create duplicates
            # with missing-method variants.
            shield_routes: list[tuple[str, dict[str, Any]]] = []
            for route in app.routes:
                if not isinstance(route, APIRoute):
                    continue
                if not hasattr(route.endpoint, "__shield_meta__"):
                    continue
                meta: dict[str, Any] = route.endpoint.__shield_meta__
                methods: set[str] = route.methods or set()
                if methods:
                    for method in sorted(methods):
                        shield_routes.append((f"{method}:{route.path}", meta))
                else:
                    shield_routes.append((route.path, meta))

            if shield_routes:
                # register_batch() is persistence-first: routes already present
                # in the cache (synced from server) are skipped.  All set_state()
                # calls queue to _pending while _startup_done is False; they are
                # flushed in a single HTTP round-trip by _flush_pending() below.
                await self._engine.register_batch(shield_routes)

            # Push any truly new routes (not already on the server) in one HTTP
            # round-trip, then mark startup complete so that subsequent
            # set_state() calls (runtime mutations) push immediately.
            await self._backend._flush_pending()

            logger.info(
                "ShieldSDK[%s]: attached — %d shield route(s) discovered",
                self._backend._app_id,
                len(shield_routes),
            )

        @app.on_event("shutdown")
        async def _shield_sdk_shutdown() -> None:
            await self._backend.shutdown()
            await self._engine.stop()
