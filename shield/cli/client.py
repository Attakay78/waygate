"""Async HTTP client for the Shield admin REST API.

All CLI commands use :func:`make_client` to obtain a :class:`ShieldClient`
configured from ``~/.shield/config.json``.  An optional ``transport``
parameter makes the client fully testable without a live server::

    transport = httpx.ASGITransport(app=admin_app)
    client = ShieldClient(base_url="http://test", transport=transport)
"""

from __future__ import annotations

import base64
from typing import Any, cast

import httpx

from shield.cli import config as _cfg


def _encode_path(path: str) -> str:
    """Base64url-encode a route path key for embedding in URL segments."""
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


class ShieldClientError(Exception):
    """Raised when the Shield server returns an error response."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        ambiguous_matches: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.ambiguous_matches: list[str] = ambiguous_matches or []


class ShieldClient:
    """Async HTTP client for the Shield admin REST API.

    Parameters
    ----------
    base_url:
        Base URL of the mounted :func:`~shield.admin.app.ShieldAdmin` app
        (e.g. ``http://localhost:8000/shield``).
    token:
        Bearer token obtained from ``POST /api/auth/login``.  When
        ``None`` unauthenticated requests are sent (works when the server
        has auth disabled).
    transport:
        Optional custom ``httpx`` transport — used in tests to point the
        client at an in-process ASGI app instead of a real server.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"X-Shield-Platform": "cli"}
        if self._token:
            headers["X-Shield-Token"] = self._token
        return headers

    def _make_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._headers(),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _check(self, resp: httpx.Response) -> Any:
        """Assert a successful response and return the parsed JSON body."""
        if resp.status_code >= 400:
            try:
                body = resp.json()
                err = body.get("error", f"HTTP {resp.status_code}")
                matches = body.get("ambiguous_matches") if isinstance(body, dict) else None
            except Exception:
                err = f"HTTP {resp.status_code}"
                matches = None
            raise ShieldClientError(err, resp.status_code, ambiguous_matches=matches)
        return resp.json()

    # ── Auth ─────────────────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """POST /api/auth/login — exchange credentials for a token."""
        async with self._make_client() as c:
            resp = await c.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            return cast(dict[str, Any], self._check(resp))

    async def logout(self) -> None:
        """POST /api/auth/logout — revoke the current bearer token."""
        async with self._make_client() as c:
            await c.post("/api/auth/logout")

    async def me(self) -> dict[str, Any]:
        """GET /api/auth/me — info about the authenticated user."""
        async with self._make_client() as c:
            resp = await c.get("/api/auth/me")
            return cast(dict[str, Any], self._check(resp))

    # ── Routes ───────────────────────────────────────────────────────────

    async def list_routes(self) -> list[dict[str, Any]]:
        """GET /api/routes — list all registered route states."""
        async with self._make_client() as c:
            resp = await c.get("/api/routes")
            return cast(list[dict[str, Any]], self._check(resp))

    async def get_route(self, path_key: str) -> dict[str, Any]:
        """GET /api/routes/{path_key} — get state for one route."""
        async with self._make_client() as c:
            resp = await c.get(f"/api/routes/{_encode_path(path_key)}")
            return cast(dict[str, Any], self._check(resp))

    async def enable(self, path_key: str, reason: str = "") -> dict[str, Any]:
        """POST /api/routes/{path_key}/enable — enable a route."""
        async with self._make_client() as c:
            resp = await c.post(
                f"/api/routes/{_encode_path(path_key)}/enable",
                json={"reason": reason},
            )
            return cast(dict[str, Any], self._check(resp))

    async def disable(self, path_key: str, reason: str = "") -> dict[str, Any]:
        """POST /api/routes/{path_key}/disable — disable a route."""
        async with self._make_client() as c:
            resp = await c.post(
                f"/api/routes/{_encode_path(path_key)}/disable",
                json={"reason": reason},
            )
            return cast(dict[str, Any], self._check(resp))

    async def maintenance(
        self,
        path_key: str,
        reason: str = "",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/routes/{path_key}/maintenance — put a route in maintenance."""
        body: dict[str, Any] = {"reason": reason}
        if start:
            body["start"] = start
        if end:
            body["end"] = end
        async with self._make_client() as c:
            resp = await c.post(
                f"/api/routes/{_encode_path(path_key)}/maintenance",
                json=body,
            )
            return cast(dict[str, Any], self._check(resp))

    async def schedule(
        self,
        path_key: str,
        start: str,
        end: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """POST /api/routes/{path_key}/schedule — schedule a maintenance window."""
        async with self._make_client() as c:
            resp = await c.post(
                f"/api/routes/{_encode_path(path_key)}/schedule",
                json={"start": start, "end": end, "reason": reason},
            )
            return cast(dict[str, Any], self._check(resp))

    async def env_gate(self, path_key: str, envs: list[str]) -> dict[str, Any]:
        """POST /api/routes/{path_key}/env — restrict a route to specific environments."""
        async with self._make_client() as c:
            resp = await c.post(
                f"/api/routes/{_encode_path(path_key)}/env",
                json={"envs": envs},
            )
            return cast(dict[str, Any], self._check(resp))

    async def cancel_schedule(self, path_key: str) -> dict[str, Any]:
        """DELETE /api/routes/{path_key}/schedule — cancel a scheduled window."""
        async with self._make_client() as c:
            resp = await c.delete(f"/api/routes/{_encode_path(path_key)}/schedule")
            return cast(dict[str, Any], self._check(resp))

    # ── Audit ────────────────────────────────────────────────────────────

    async def audit_log(self, route: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """GET /api/audit — return audit log entries."""
        params: dict[str, Any] = {"limit": limit}
        if route:
            params["route"] = route
        async with self._make_client() as c:
            resp = await c.get("/api/audit", params=params)
            return cast(list[dict[str, Any]], self._check(resp))

    # ── Global maintenance ────────────────────────────────────────────────

    async def global_status(self) -> dict[str, Any]:
        """GET /api/global — global maintenance configuration."""
        async with self._make_client() as c:
            resp = await c.get("/api/global")
            return cast(dict[str, Any], self._check(resp))

    async def global_enable(
        self,
        reason: str = "",
        exempt_paths: list[str] | None = None,
        include_force_active: bool = False,
    ) -> dict[str, Any]:
        """POST /api/global/enable — enable global maintenance mode."""
        async with self._make_client() as c:
            resp = await c.post(
                "/api/global/enable",
                json={
                    "reason": reason,
                    "exempt_paths": exempt_paths or [],
                    "include_force_active": include_force_active,
                },
            )
            return cast(dict[str, Any], self._check(resp))

    async def global_disable(self) -> dict[str, Any]:
        """POST /api/global/disable — disable global maintenance mode."""
        async with self._make_client() as c:
            resp = await c.post("/api/global/disable")
            return cast(dict[str, Any], self._check(resp))

    async def list_rate_limits(self) -> list[dict[str, Any]]:
        """GET /api/rate-limits — list all rate limit policies."""
        async with self._make_client() as c:
            resp = await c.get("/api/rate-limits")
            return cast(list[dict[str, Any]], self._check(resp))

    async def rate_limit_hits(
        self, route: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """GET /api/rate-limits/hits — recent rate limit hits."""
        params: dict[str, str | int] = {"limit": limit}
        if route:
            params["route"] = route
        async with self._make_client() as c:
            resp = await c.get("/api/rate-limits/hits", params=params)
            return cast(list[dict[str, Any]], self._check(resp))

    async def reset_rate_limit(self, path_key: str, method: str | None = None) -> dict[str, Any]:
        """DELETE /api/rate-limits/{path_key}/reset — reset rate limit counters."""
        params: dict[str, str] = {}
        if method:
            params["method"] = method
        async with self._make_client() as c:
            resp = await c.delete(f"/api/rate-limits/{path_key}/reset", params=params)
            return cast(dict[str, Any], self._check(resp))

    async def set_rate_limit_policy(
        self,
        path: str,
        method: str,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        burst: int = 0,
    ) -> dict[str, Any]:
        """POST /api/rate-limits — create or update a rate limit policy."""
        payload: dict[str, Any] = {
            "path": path,
            "method": method.upper(),
            "limit": limit,
            "burst": burst,
        }
        if algorithm:
            payload["algorithm"] = algorithm
        if key_strategy:
            payload["key_strategy"] = key_strategy
        async with self._make_client() as c:
            resp = await c.post("/api/rate-limits", json=payload)
            return cast(dict[str, Any], self._check(resp))

    async def delete_rate_limit_policy(self, path: str, method: str) -> dict[str, Any]:
        """DELETE /api/rate-limits/{path_key} — remove a rate limit policy."""
        composite = f"{method.upper()}:{path}"
        path_key = _encode_path(composite)
        async with self._make_client() as c:
            resp = await c.delete(f"/api/rate-limits/{path_key}")
            return cast(dict[str, Any], self._check(resp))

    # ------------------------------------------------------------------
    # Global rate limit
    # ------------------------------------------------------------------

    async def get_global_rate_limit(self) -> dict[str, Any]:
        """GET /api/global-rate-limit — current global rate limit policy."""
        async with self._make_client() as c:
            resp = await c.get("/api/global-rate-limit")
            return cast(dict[str, Any], self._check(resp))

    async def set_global_rate_limit(
        self,
        limit: str,
        *,
        algorithm: str | None = None,
        key_strategy: str | None = None,
        burst: int = 0,
        exempt_routes: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/global-rate-limit — set or update the global rate limit policy."""
        payload: dict[str, Any] = {
            "limit": limit,
            "burst": burst,
            "exempt_routes": exempt_routes or [],
        }
        if algorithm:
            payload["algorithm"] = algorithm
        if key_strategy:
            payload["key_strategy"] = key_strategy
        async with self._make_client() as c:
            resp = await c.post("/api/global-rate-limit", json=payload)
            return cast(dict[str, Any], self._check(resp))

    async def delete_global_rate_limit(self) -> dict[str, Any]:
        """DELETE /api/global-rate-limit — remove the global rate limit policy."""
        async with self._make_client() as c:
            resp = await c.delete("/api/global-rate-limit")
            return cast(dict[str, Any], self._check(resp))

    async def reset_global_rate_limit(self) -> dict[str, Any]:
        """DELETE /api/global-rate-limit/reset — reset global rate limit counters."""
        async with self._make_client() as c:
            resp = await c.delete("/api/global-rate-limit/reset")
            return cast(dict[str, Any], self._check(resp))

    async def enable_global_rate_limit(self) -> dict[str, Any]:
        """POST /api/global-rate-limit/enable — resume a paused global rate limit."""
        async with self._make_client() as c:
            resp = await c.post("/api/global-rate-limit/enable")
            return cast(dict[str, Any], self._check(resp))

    async def disable_global_rate_limit(self) -> dict[str, Any]:
        """POST /api/global-rate-limit/disable — pause the global rate limit."""
        async with self._make_client() as c:
            resp = await c.post("/api/global-rate-limit/disable")
            return cast(dict[str, Any], self._check(resp))


def make_client(
    transport: httpx.AsyncBaseTransport | None = None,
) -> ShieldClient:
    """Create a :class:`ShieldClient` from the local CLI config.

    Reads ``server_url`` and ``auth.token`` from ``~/.shield/config.json``.
    Exits with a helpful message when the server URL is not configured.

    Parameters
    ----------
    transport:
        Optional transport override — useful in tests to avoid a real server.
    """
    server_url = _cfg.require_server_url()
    token = _cfg.get_auth_token()
    return ShieldClient(base_url=server_url, token=token, transport=transport)
