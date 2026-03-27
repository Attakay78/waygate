"""Tests for the rate-limit dashboard features.

Covers:
- _get_unrated_routes() unit tests
- /rate-limits page rendering (unprotected routes section)
- /modal/rl/add/{path_key} modal GET
- /rl/add POST (create policy, redirect, unknown-route error)
"""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from shield.admin.app import ShieldAdmin
from shield.core.engine import ShieldEngine
from shield.core.models import RouteState, RouteStatus
from shield.core.rate_limit.storage import HAS_LIMITS
from shield.dashboard.routes import _get_unrated_routes

# ---------------------------------------------------------------------------
# Skip marker — skip every rate-limit test when the limits library is absent
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_path(path: str) -> str:
    """Base64url-encode *path* for use in a URL segment (mirrors routes.py)."""
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def _make_state(path: str, service: str = "") -> RouteState:
    """Return a minimal active RouteState for *path*."""
    return RouteState(path=path, status=RouteStatus.ACTIVE, service=service or None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> ShieldEngine:
    """Provide a ShieldEngine pre-loaded with two test routes."""
    e = ShieldEngine()
    await e.backend.set_state(
        "/api/items", RouteState(path="/api/items", status=RouteStatus.ACTIVE)
    )
    await e.backend.set_state(
        "/api/orders", RouteState(path="/api/orders", status=RouteStatus.ACTIVE)
    )
    return e


@pytest.fixture
def admin(engine: ShieldEngine) -> object:
    """Return a ShieldAdmin ASGI app (no auth)."""
    return ShieldAdmin(engine=engine)


@pytest.fixture
async def client(admin: object) -> AsyncClient:
    """Return an httpx AsyncClient pointing at the ShieldAdmin app."""
    async with AsyncClient(
        transport=ASGITransport(app=admin),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests for _get_unrated_routes
# ---------------------------------------------------------------------------


def test_get_unrated_routes_returns_all_when_no_policies() -> None:
    """All routes are unrated when no policies exist."""
    states = [_make_state("/api/items"), _make_state("/api/orders")]
    result = _get_unrated_routes(states, {}, "")
    assert len(result) == 2
    paths = {s.path for s in result}
    assert "/api/items" in paths
    assert "/api/orders" in paths


def test_get_unrated_routes_excludes_rated() -> None:
    """A route that has a policy is excluded from the unrated list."""
    states = [_make_state("/api/items"), _make_state("/api/orders")]
    # Policy key format is "METHOD:/path"
    policies = {"GET:/api/items": object()}
    result = _get_unrated_routes(states, policies, "")
    paths = {s.path for s in result}
    assert "/api/items" not in paths
    assert "/api/orders" in paths


def test_get_unrated_routes_excludes_method_prefixed_rated() -> None:
    """Routes stored as 'METHOD:/path' are excluded when a policy exists for that path."""
    # scan_routes registers routes with method prefix: "GET:/api/items"
    states = [
        _make_state("GET:/api/items"),
        _make_state("POST:/api/items"),
        _make_state("GET:/api/orders"),
    ]
    policies = {"GET:/api/items": object()}
    result = _get_unrated_routes(states, policies, "")
    paths = {s.path for s in result}
    # GET:/api/items is rated — excluded
    assert "GET:/api/items" not in paths
    # POST:/api/items shares the same path — also excluded (same bare path "/api/items")
    assert "POST:/api/items" not in paths
    # GET:/api/orders has no policy — included
    assert "GET:/api/orders" in paths


def test_get_unrated_routes_service_filter() -> None:
    """With service filter, only routes matching that service are included."""
    states = [
        _make_state("/api/items", service="payments"),
        _make_state("/api/orders", service="fulfillment"),
        _make_state("/api/users", service="payments"),
    ]
    result = _get_unrated_routes(states, {}, "payments")
    services = {s.service for s in result}
    assert services == {"payments"}
    paths = {s.path for s in result}
    assert "/api/orders" not in paths


# ---------------------------------------------------------------------------
# Integration tests — ShieldAdmin /rate-limits page
# ---------------------------------------------------------------------------


async def test_rate_limits_page_includes_unprotected_section(
    client: AsyncClient,
) -> None:
    """GET /rate-limits returns 200 and includes the 'Unprotected Routes' section
    when routes with no rate limit policy exist."""
    resp = await client.get("/rate-limits")
    assert resp.status_code == 200
    assert "Unprotected Routes" in resp.text


async def test_rate_limits_page_no_unprotected_when_all_rated(
    engine: ShieldEngine, admin: object
) -> None:
    """When every route has a policy, 'Unprotected Routes' should not appear."""
    # Add policies for both registered routes.
    await engine.set_rate_limit_policy(
        path="/api/items", method="GET", limit="10/minute", actor="test"
    )
    await engine.set_rate_limit_policy(
        path="/api/orders", method="GET", limit="10/minute", actor="test"
    )

    async with AsyncClient(
        transport=ASGITransport(app=admin),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        resp = await c.get("/rate-limits")

    assert resp.status_code == 200
    assert "Unprotected Routes" not in resp.text


# ---------------------------------------------------------------------------
# Integration tests — modal GET /modal/rl/add/{path_key}
# ---------------------------------------------------------------------------


async def test_modal_rl_add_returns_200(client: AsyncClient) -> None:
    """GET /modal/rl/add/{path_key} returns 200 with the add-policy form."""
    path_key = _encode_path("/api/items")
    resp = await client.get(f"/modal/rl/add/{path_key}")
    assert resp.status_code == 200
    assert "Add Rate Limit Policy" in resp.text


# ---------------------------------------------------------------------------
# Integration tests — POST /rl/add
# ---------------------------------------------------------------------------


async def test_rl_add_creates_policy_and_redirects(engine: ShieldEngine, admin: object) -> None:
    """POST /rl/add with a registered path creates the policy and returns 204
    with an HX-Redirect header pointing at /rate-limits."""
    async with AsyncClient(
        transport=ASGITransport(app=admin),  # type: ignore[arg-type]
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        resp = await c.post(
            "/rl/add",
            data={
                "path": "/api/items",
                "method": "GET",
                "limit": "10/minute",
            },
        )

    assert resp.status_code == 204
    assert "HX-Redirect" in resp.headers
    assert "/rate-limits" in resp.headers["HX-Redirect"]
    # Confirm the policy was actually persisted in the engine.
    assert "GET:/api/items" in engine._rate_limit_policies


async def test_rl_add_unknown_route_returns_error(admin: object) -> None:
    """POST /rl/add for a path that is not registered in the engine rejects
    the request — the handler raises RouteNotFoundException (propagates as a
    server error since rl_add does not catch it)."""
    from shield.core.exceptions import RouteNotFoundException

    async with AsyncClient(
        transport=ASGITransport(app=admin),  # type: ignore[arg-type]
        base_url="http://testserver",
        follow_redirects=False,
        # Allow the exception to propagate so we can assert its type.
    ) as c:
        with pytest.raises(RouteNotFoundException):
            await c.post(
                "/rl/add",
                data={
                    "path": "/does/not/exist",
                    "method": "GET",
                    "limit": "5/minute",
                },
            )
