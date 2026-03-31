"""WaygateServer — standalone Waygate Server factory.

Deploy this once as its own service.  All your FastAPI applications
connect to it via :class:`~waygate.sdk.WaygateSDK`.

Usage::

    # waygate_server.py
    from waygate.server import WaygateServer
    from waygate.core.backends.redis import RedisBackend

    app = WaygateServer(
        backend=RedisBackend("redis://localhost:6379"),
        auth=("admin", "secret"),
    )

    # Run with: uvicorn waygate_server:app

Then in each service::

    from waygate.sdk import WaygateSDK

    sdk = WaygateSDK(
        server_url="http://waygate-server:9000",
        app_id="payments-service",
        token="...",
    )
    sdk.attach(app)

And point the CLI at the server::

    waygate config set-url http://waygate-server:9000
    waygate login admin
    waygate status

Use ``RedisBackend`` so every connected service receives live state
updates via the SSE channel.  ``MemoryBackend`` works for local
development with a single service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from waygate.admin.auth import AuthConfig
    from waygate.core.backends.base import WaygateBackend

__all__ = ["WaygateServer"]


def WaygateServer(
    backend: WaygateBackend,
    auth: AuthConfig = None,
    token_expiry: int = 86400,
    sdk_token_expiry: int = 31536000,
    secret_key: str | None = None,
    prefix: str = "",
) -> ASGIApp:
    """Create a standalone Waygate Server ASGI application.

    The returned app exposes the full :class:`~waygate.admin.app.WaygateAdmin`
    surface: dashboard UI, REST API, and the SDK SSE/register endpoints.

    Parameters
    ----------
    backend:
        Storage backend.  Use :class:`~waygate.core.backends.redis.RedisBackend`
        for multi-service deployments — Redis pub/sub ensures every SDK
        client receives live updates when state changes.
        :class:`~waygate.core.backends.memory.MemoryBackend` is fine for
        local development with a single service.
    auth:
        Credentials config — same as :func:`~waygate.admin.app.WaygateAdmin`:

        * ``None`` — open access (no credentials required)
        * ``("user", "pass")`` — single user
        * ``[("u1", "p1"), ("u2", "p2")]`` — multiple users
        * :class:`~waygate.admin.auth.WaygateAuthBackend` instance — custom
    token_expiry:
        Token lifetime in seconds for dashboard and CLI users.
        Default: 86400 (24 h).
    sdk_token_expiry:
        Token lifetime in seconds for SDK service tokens.
        Default: 31536000 (1 year).  Service apps that authenticate with
        ``username``/``password`` via :class:`~waygate.sdk.WaygateSDK`
        receive a token of this duration so they never need manual
        re-authentication.
    secret_key:
        HMAC signing key.  Use a stable value so tokens survive process
        restarts.  Defaults to a random key (tokens invalidated on restart).
    prefix:
        URL prefix if the server app is mounted under a sub-path.
        Usually empty when running as a standalone service.

    Returns
    -------
    ASGIApp
        A Starlette ASGI application ready to be served by uvicorn /
        gunicorn, or mounted on another app.
    """
    from waygate.admin.app import WaygateAdmin
    from waygate.core.engine import WaygateEngine

    engine = WaygateEngine(backend=backend)
    return WaygateAdmin(
        engine=engine,
        auth=auth,
        token_expiry=token_expiry,
        sdk_token_expiry=sdk_token_expiry,
        secret_key=secret_key,
        prefix=prefix,
    )
