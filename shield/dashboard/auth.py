"""Optional HTTP Basic Auth middleware for the Shield dashboard."""

from __future__ import annotations

import base64
import binascii

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Wrap the dashboard Starlette app with HTTP Basic Authentication.

    When *auth* credentials are provided to :func:`ShieldDashboard`, this
    middleware is added to the inner app.  All requests must carry a valid
    ``Authorization: Basic …`` header, otherwise a ``401`` challenge is
    returned.

    Parameters
    ----------
    app:
        The ASGI application to protect.
    username:
        Expected username.
    password:
        Expected password.
    """

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username
        self._password = password

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate Basic Auth credentials before passing the request through."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return self._challenge()

        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return self._challenge()

        if username != self._username or password != self._password:
            return self._challenge()

        return await call_next(request)

    @staticmethod
    def _challenge() -> Response:
        """Return a 401 Unauthorized response with a WWW-Authenticate challenge."""
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Shield Dashboard"'},
            content="Unauthorized",
        )
