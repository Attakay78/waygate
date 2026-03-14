"""Integration tests for FastAPI dependency injection helpers.

Covers:
- ``ShieldGuard`` (engine-backed class-based dep)
- Decorator factories (``maintenance``, ``disabled``, ``env_only``) used as
  ``Depends()`` arguments in two modes:
  - Inline / stateless (no engine=) — always raises the declared error
  - Engine-backed (engine=engine) — calls engine.check(); runtime-togglable
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import MaintenanceWindow
from shield.fastapi.decorators import disabled, env_only, maintenance
from shield.fastapi.dependencies import ShieldGuard, configure_shield


def _engine(env: str = "production") -> ShieldEngine:
    return ShieldEngine(backend=MemoryBackend(), current_env=env)


# ---------------------------------------------------------------------------
# ShieldGuard — engine-backed dependency
# ---------------------------------------------------------------------------


class TestShieldGuard:
    async def test_active_route_passes_through(self):
        engine = _engine()
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(ShieldGuard(engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/orders")

        assert resp.status_code == 200

    async def test_maintenance_state_returns_503(self):
        engine = _engine()
        await engine.set_maintenance("/orders", reason="Reindex")
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(ShieldGuard(engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/orders")

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "MAINTENANCE_MODE"
        assert detail["reason"] == "Reindex"
        assert detail["path"] == "/orders"

    async def test_maintenance_with_window_sets_retry_after(self):
        engine = _engine()
        end = datetime(2099, 1, 1, tzinfo=UTC)
        await engine.set_maintenance(
            "/orders",
            reason="Migration",
            window=MaintenanceWindow(start=datetime(2099, 1, 1, tzinfo=UTC), end=end),
        )
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(ShieldGuard(engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/orders")

        assert resp.status_code == 503
        assert "retry-after" in resp.headers

    async def test_disabled_state_returns_503(self):
        engine = _engine()
        await engine.disable("/orders", reason="Deprecated endpoint")
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(ShieldGuard(engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/orders")

        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "ROUTE_DISABLED"

    async def test_env_gated_wrong_env_returns_404(self):
        engine = _engine(env="production")
        await engine.set_env_only("/debug", envs=["dev", "staging"])
        app = FastAPI()

        @app.get("/debug", dependencies=[Depends(ShieldGuard(engine))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 404

    async def test_env_gated_correct_env_passes_through(self):
        engine = _engine(env="dev")
        await engine.set_env_only("/debug", envs=["dev"])
        app = FastAPI()

        @app.get("/debug", dependencies=[Depends(ShieldGuard(engine))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 200

    async def test_state_change_reflected_immediately(self):
        engine = _engine()
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(ShieldGuard(engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            first = await c.get("/orders")
            await engine.set_maintenance("/orders", reason="Live toggle")
            second = await c.get("/orders")
            await engine.enable("/orders")
            third = await c.get("/orders")

        assert first.status_code == 200
        assert second.status_code == 503
        assert third.status_code == 200

    async def test_composable_with_auth_dependency(self):
        from fastapi import Header
        from fastapi import HTTPException as FastAPIHTTPException

        engine = _engine()
        await engine.disable("/admin", reason="Under construction")

        async def require_key(x_api_key: str | None = Header(default=None)) -> None:
            if x_api_key != "secret":
                raise FastAPIHTTPException(status_code=401)

        app = FastAPI()

        @app.get("/admin", dependencies=[Depends(require_key), Depends(ShieldGuard(engine))])
        async def admin():
            return {"admin": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            no_key = await c.get("/admin")
            with_key = await c.get("/admin", headers={"X-Api-Key": "secret"})

        assert no_key.status_code == 401  # auth fires before shield
        assert with_key.status_code == 503  # auth passes, shield blocks


# ---------------------------------------------------------------------------
# maintenance() used as a Depends() argument
# ---------------------------------------------------------------------------


class TestMaintenanceAsDep:
    async def test_always_returns_503(self):
        app = FastAPI()

        @app.get("/payments", dependencies=[Depends(maintenance(reason="DB migration"))])
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "MAINTENANCE_MODE"
        assert detail["reason"] == "DB migration"
        assert detail["path"] == "/payments"

    async def test_sets_retry_after_when_end_provided(self):
        end = datetime(2099, 6, 1, tzinfo=UTC)
        app = FastAPI()

        @app.get("/payments", dependencies=[Depends(maintenance(reason="Migration", end=end))])
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 503
        assert "retry-after" in resp.headers
        assert "retry_after" in resp.json()["detail"]

    async def test_no_retry_after_without_end(self):
        app = FastAPI()

        @app.get("/payments", dependencies=[Depends(maintenance())])
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 503
        assert "retry-after" not in resp.headers

    async def test_time_window_passes_through_outside_window(self):
        # Window entirely in the past → outside window → request passes through
        past_start = datetime(2000, 1, 1, tzinfo=UTC)
        past_end = datetime(2000, 1, 2, tzinfo=UTC)
        app = FastAPI()

        @app.get(
            "/payments",
            dependencies=[
                Depends(maintenance(reason="Old window", start=past_start, end=past_end))
            ],
        )
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 200

    async def test_same_object_works_as_decorator(self):
        """The same maintenance() call stamps __shield_meta__ when used as a decorator."""
        guard = maintenance(reason="Migration")

        @guard
        async def endpoint():
            return {"ok": True}

        assert endpoint.__shield_meta__["status"] == "maintenance"
        assert endpoint.__shield_meta__["reason"] == "Migration"
        assert await endpoint() == {"ok": True}


# ---------------------------------------------------------------------------
# env_only() used as a Depends() argument
# ---------------------------------------------------------------------------


class TestEnvOnlyAsDep:
    async def test_wrong_env_returns_404(self):
        engine = _engine(env="production")
        app = FastAPI()

        @app.get("/debug", dependencies=[Depends(env_only("dev", "staging", engine=engine))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 404

    async def test_correct_env_passes_through(self):
        engine = _engine(env="staging")
        app = FastAPI()

        @app.get("/debug", dependencies=[Depends(env_only("dev", "staging", engine=engine))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 200

    async def test_same_object_works_as_decorator(self):
        engine = _engine(env="dev")
        guard = env_only("dev", "staging", engine=engine)

        @guard
        async def endpoint():
            return {"ok": True}

        assert endpoint.__shield_meta__["status"] == "env_gated"
        assert endpoint.__shield_meta__["allowed_envs"] == ["dev", "staging"]


# ---------------------------------------------------------------------------
# disabled() used as a Depends() argument
# ---------------------------------------------------------------------------


class TestDisabledAsDep:
    async def test_always_returns_503(self):
        app = FastAPI()

        @app.get("/old", dependencies=[Depends(disabled(reason="Use /v2"))])
        async def old():
            return {}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/old")

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "ROUTE_DISABLED"
        assert detail["reason"] == "Use /v2"
        assert detail["path"] == "/old"

    async def test_empty_reason(self):
        app = FastAPI()

        @app.get("/gone", dependencies=[Depends(disabled())])
        async def gone():
            return {}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/gone")

        assert resp.status_code == 503
        assert resp.json()["detail"]["reason"] == ""

    async def test_same_object_works_as_decorator(self):
        guard = disabled(reason="Use /v2")

        @guard
        async def endpoint():
            return {"ok": True}

        assert endpoint.__shield_meta__["status"] == "disabled"
        assert endpoint.__shield_meta__["reason"] == "Use /v2"


# ---------------------------------------------------------------------------
# Engine-backed deps — explicit engine= (backward compat)
# ---------------------------------------------------------------------------


class TestEngineBackedDeps:
    """maintenance/disabled/env_only with engine= call engine.check() at runtime."""

    async def test_maintenance_engine_enforces_backend_state(self):
        engine = _engine()
        await engine.set_maintenance("/payments", reason="Migration")
        app = FastAPI()

        @app.get(
            "/payments",
            dependencies=[Depends(maintenance(reason="Migration", engine=engine))],
        )
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            blocked = await c.get("/payments")
            await engine.enable("/payments")
            allowed = await c.get("/payments")

        assert blocked.status_code == 503
        assert blocked.json()["detail"]["code"] == "MAINTENANCE_MODE"
        assert allowed.status_code == 200

    async def test_disabled_engine_can_be_reenabled_at_runtime(self):
        engine = _engine()
        await engine.disable("/old", reason="Deprecated")
        app = FastAPI()

        @app.get("/old", dependencies=[Depends(disabled(reason="Deprecated", engine=engine))])
        async def old():
            return {"old": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            blocked = await c.get("/old")
            await engine.enable("/old")
            allowed = await c.get("/old")

        assert blocked.status_code == 503
        assert allowed.status_code == 200

    async def test_env_only_engine_checks_current_env_inline(self):
        engine = _engine(env="production")
        app = FastAPI()

        @app.get("/debug", dependencies=[Depends(env_only("dev", engine=engine))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 404

    async def test_engine_backed_reflects_state_change_immediately(self):
        engine = _engine()
        app = FastAPI()

        @app.get("/orders", dependencies=[Depends(maintenance(reason="Start", engine=engine))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            first = await c.get("/orders")
            await engine.set_maintenance("/orders", reason="Live update")
            second = await c.get("/orders")
            await engine.enable("/orders")
            third = await c.get("/orders")

        assert first.status_code == 200
        assert second.status_code == 503
        assert third.status_code == 200

    async def test_engine_backed_stamps_meta_for_shieldrouter(self):
        engine = _engine()
        guard = maintenance(reason="Migration", engine=engine)

        @guard
        async def endpoint():
            return {"ok": True}

        assert endpoint.__shield_meta__["status"] == "maintenance"
        assert endpoint.__shield_meta__["reason"] == "Migration"
        assert await endpoint() == {"ok": True}


# ---------------------------------------------------------------------------
# configure_shield — zero-config: no engine= per route
# ---------------------------------------------------------------------------


class TestConfigureShield:
    """With configure_shield(app, engine) called once, all decorator deps find
    the engine automatically via request.app.state.shield_engine."""

    async def test_maintenance_resolves_engine_from_app_state(self):
        engine = _engine()
        await engine.set_maintenance("/payments", reason="Migration")
        app = FastAPI()
        configure_shield(app, engine)  # once — no engine= on the dep

        @app.get("/payments", dependencies=[Depends(maintenance(reason="Migration"))])
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            blocked = await c.get("/payments")
            await engine.enable("/payments")
            allowed = await c.get("/payments")

        assert blocked.status_code == 503
        assert allowed.status_code == 200

    async def test_disabled_resolves_engine_from_app_state(self):
        engine = _engine()
        await engine.disable("/old", reason="Gone")
        app = FastAPI()
        configure_shield(app, engine)

        @app.get("/old", dependencies=[Depends(disabled(reason="Gone"))])
        async def old():
            return {}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            blocked = await c.get("/old")
            await engine.enable("/old")
            allowed = await c.get("/old")

        assert blocked.status_code == 503
        assert allowed.status_code == 200

    async def test_env_only_resolves_engine_from_app_state(self):
        engine = _engine(env="production")
        app = FastAPI()
        configure_shield(app, engine)

        @app.get("/debug", dependencies=[Depends(env_only("dev", "staging"))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            prod = await c.get("/debug")

        assert prod.status_code == 404

    async def test_env_only_passes_in_allowed_env(self):
        engine = _engine(env="dev")
        app = FastAPI()
        configure_shield(app, engine)

        @app.get("/debug", dependencies=[Depends(env_only("dev", "staging"))])
        async def debug():
            return {"debug": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/debug")

        assert resp.status_code == 200

    async def test_without_configure_shield_falls_back_to_inline(self):
        """No configure_shield and no engine= → inline always-block behavior."""
        app = FastAPI()

        @app.get("/payments", dependencies=[Depends(maintenance(reason="Always blocked"))])
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 503  # inline always-block, no engine

    async def test_explicit_engine_takes_priority_over_app_state(self):
        """Explicit engine= overrides app.state.shield_engine."""
        app_engine = _engine()  # engine on app state — route is ACTIVE here
        explicit_engine = _engine()
        await explicit_engine.set_maintenance("/payments", reason="Explicit")

        app = FastAPI()
        configure_shield(app, app_engine)  # ACTIVE for /payments

        @app.get(
            "/payments",
            # explicit_engine has /payments in maintenance — overrides app_engine
            dependencies=[Depends(maintenance(reason="Explicit", engine=explicit_engine))],
        )
        async def payments():
            return {"payments": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/payments")

        assert resp.status_code == 503  # explicit engine wins

    async def test_multiple_routes_same_app_all_registered(self):
        """configure_shield enables engine-backed deps for ALL routes on the app.
        Both routes must be registered in the engine for enforcement to take effect."""
        engine = _engine()
        await engine.set_maintenance("/payments", reason="Migration")
        await engine.disable("/users", reason="Gone")  # must register for dep to enforce

        app = FastAPI()
        configure_shield(app, engine)

        @app.get("/payments", dependencies=[Depends(maintenance(reason="Migration"))])
        async def payments():
            return {"payments": []}

        @app.get("/users", dependencies=[Depends(disabled(reason="Gone"))])
        async def users():
            return {"users": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            payments_resp = await c.get("/payments")
            users_resp = await c.get("/users")

        assert payments_resp.status_code == 503
        assert users_resp.status_code == 503

    async def test_unregistered_route_is_fail_open_with_engine(self):
        """If a route is NOT registered in the engine, engine.check() returns ACTIVE.
        Use the decorator + ShieldRouter to register initial state at startup."""
        engine = _engine()
        # /orders NOT registered in engine — engine.check returns ACTIVE

        app = FastAPI()
        configure_shield(app, engine)

        @app.get("/orders", dependencies=[Depends(maintenance(reason="Ignored"))])
        async def orders():
            return {"orders": []}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/orders")

        assert resp.status_code == 200  # fail-open: no state in engine
