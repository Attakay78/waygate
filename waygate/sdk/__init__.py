"""WaygateSDK — connect a FastAPI app to a remote Waygate Server.

Drop-in alternative to the embedded setup.  State is managed centrally
from the Waygate Server dashboard or CLI; this SDK enforces it locally on
every request with zero network overhead.

Usage::

    from waygate.sdk import WaygateSDK

    sdk = WaygateSDK(
        server_url="http://waygate-server:9000",
        app_id="payments-service",
        token="...",    # omit if server has no auth
    )
    sdk.attach(app)

    @app.get("/payments")
    @maintenance(reason="DB migration")   # optional — manage from dashboard instead
    async def payments():
        return {"ok": True}

The CLI then points at the Waygate Server, not at this service::

    waygate config set-url http://waygate-server:9000
    waygate status                         # routes from ALL connected services
    waygate disable payments-service /api/payments --reason "migration"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from waygate.core.backends.base import WaygateBackend
from waygate.core.backends.server import WaygateServerBackend
from waygate.core.engine import WaygateEngine

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["WaygateSDK"]

logger = logging.getLogger(__name__)


class WaygateSDK:
    """Connect a FastAPI application to a remote Waygate Server.

    Parameters
    ----------
    server_url:
        Base URL of the Waygate Server (e.g. ``http://waygate-server:9000``).
        If the Waygate Server is mounted under a prefix (e.g. ``/waygate``),
        include the prefix: ``http://myapp.com/waygate``.
    app_id:
        Unique name for this service shown in the Waygate Server dashboard.
        Use a stable identifier like ``"payments-service"`` or
        ``"orders-api"``.
    token:
        Pre-issued bearer token for Waygate Server auth.  Takes priority
        over ``username``/``password`` if both are provided.  Omit if
        the server has no auth configured.
    username:
        Waygate Server username.  When provided alongside ``password``
        (and no ``token``), the SDK automatically calls
        ``POST /api/auth/login`` on startup with ``platform="sdk"`` and
        obtains a long-lived service token — no manual token management
        required.  Store credentials in environment variables and inject
        them at deploy time::

            sdk = WaygateSDK(
                server_url=os.environ["WAYGATE_SERVER_URL"],
                app_id="payments-service",
                username=os.environ["WAYGATE_USERNAME"],
                password=os.environ["WAYGATE_PASSWORD"],
            )
    password:
        Waygate Server password.  Used together with ``username``.
    reconnect_delay:
        Seconds between SSE reconnect attempts after a dropped connection.
        Defaults to 5 seconds.
    rate_limit_backend:
        Optional shared backend for rate limit counter storage.  When
        ``None`` (default) each instance maintains its own in-process
        counters — a ``100/minute`` limit is enforced independently on
        each replica.  Pass a :class:`~waygate.core.backends.redis.RedisBackend`
        pointing at a shared Redis instance to enforce the limit
        **across all replicas combined**::

            from waygate.core.backends.redis import RedisBackend

            sdk = WaygateSDK(
                server_url="http://waygate:9000",
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
        rate_limit_backend: WaygateBackend | None = None,
    ) -> None:
        self._backend = WaygateServerBackend(
            server_url=server_url,
            app_id=app_id,
            token=token,
            username=username,
            password=password,
            reconnect_delay=reconnect_delay,
        )
        self._engine = WaygateEngine(
            backend=self._backend,
            rate_limit_backend=rate_limit_backend,
        )

    @property
    def engine(self) -> WaygateEngine:
        """The underlying :class:`~waygate.core.engine.WaygateEngine`.

        Use this if you need direct engine access (e.g. to call
        ``engine.disable()`` programmatically from within the service).
        """
        return self._engine

    def use_openfeature(
        self,
        hooks: list[Any] | None = None,
        domain: str = "waygate",
    ) -> None:
        """Enable OpenFeature feature-flag evaluation for this SDK client.

        Must be called **before** :meth:`attach`.

        Activates :class:`~waygate.sdk.flag_provider.WaygateSDKFlagProvider`
        which:

        * On startup fetches all flags/segments from the Waygate Server via
          ``GET /api/flags`` and ``GET /api/segments``.
        * Stays current by listening to ``flag_updated``, ``flag_deleted``,
          ``segment_updated``, and ``segment_deleted`` events on the
          existing SSE connection — no extra network connections needed.

        Usage::

            sdk = WaygateSDK(server_url="http://waygate:9000", app_id="my-svc")
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
            OpenFeature provider domain name (default ``"waygate"``).
        """
        from waygate.sdk.flag_provider import WaygateSDKFlagProvider

        provider = WaygateSDKFlagProvider(self._backend)
        self._engine.use_openfeature(provider=provider, hooks=hooks, domain=domain)

    def attach(self, app: FastAPI) -> None:
        """Wire waygate middleware and lifecycle hooks into *app*.

        Call this once after creating the FastAPI app and before
        defining routes::

            sdk.attach(app)

            @app.get("/payments")
            async def payments():
                ...

        What ``attach`` does:

        1. Adds :class:`~waygate.fastapi.middleware.WaygateMiddleware` so
           every request is checked against the local state cache.
        2. On startup: syncs state from the Waygate Server, starts the SSE
           listener, discovers decorated routes, and registers any new
           ones with the server.
        3. On shutdown: closes the SSE connection and HTTP client cleanly.

        Parameters
        ----------
        app:
            The :class:`fastapi.FastAPI` application to attach to.
        """
        from fastapi.routing import APIRoute

        from waygate.fastapi.middleware import WaygateMiddleware

        app.add_middleware(WaygateMiddleware, engine=self._engine)

        @app.on_event("startup")
        async def _waygate_sdk_startup() -> None:
            # Start engine background tasks (pub/sub listeners, etc.)
            await self._engine.start()
            # Connect to Waygate Server: sync state + open SSE stream.
            await self._backend.startup()

            # Discover routes decorated with @maintenance, @disabled, etc.
            # and register any that are new to the Waygate Server.
            # Use the same method-prefixed key format as WaygateRouter
            # (e.g. "GET:/api/payments") so that routes registered by
            # WaygateRouter before the SDK startup don't create duplicates
            # with missing-method variants.
            waygate_routes: list[tuple[str, dict[str, Any]]] = []
            for route in app.routes:
                if not isinstance(route, APIRoute):
                    continue
                if not hasattr(route.endpoint, "__waygate_meta__"):
                    continue
                meta: dict[str, Any] = route.endpoint.__waygate_meta__
                methods: set[str] = route.methods or set()
                if methods:
                    for method in sorted(methods):
                        waygate_routes.append((f"{method}:{route.path}", meta))
                else:
                    waygate_routes.append((route.path, meta))

            if waygate_routes:
                # register_batch() is persistence-first: routes already present
                # in the cache (synced from server) are skipped.  All set_state()
                # calls queue to _pending while _startup_done is False; they are
                # flushed in a single HTTP round-trip by _flush_pending() below.
                await self._engine.register_batch(waygate_routes)

            # Push any truly new routes (not already on the server) in one HTTP
            # round-trip, then mark startup complete so that subsequent
            # set_state() calls (runtime mutations) push immediately.
            await self._backend._flush_pending()

            logger.info(
                "WaygateSDK[%s]: attached — %d waygate route(s) discovered",
                self._backend._app_id,
                len(waygate_routes),
            )

        @app.on_event("shutdown")
        async def _waygate_sdk_shutdown() -> None:
            await self._backend.shutdown()
            await self._engine.stop()
