"""Tests for rate limit policy persistence across all backends + engine."""

from __future__ import annotations

import pytest

from waygate.core.rate_limit.storage import HAS_LIMITS

pytestmark = pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


# ---------------------------------------------------------------------------
# Backend-level persistence tests
# ---------------------------------------------------------------------------


class TestMemoryBackendPolicyPersistence:
    async def test_set_and_get(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        policies = await backend.get_rate_limit_policies()
        assert len(policies) == 1
        assert policies[0]["limit"] == "10/minute"

    async def test_overwrite(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "50/minute"})
        policies = await backend.get_rate_limit_policies()
        assert len(policies) == 1
        assert policies[0]["limit"] == "50/minute"

    async def test_delete(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        await backend.delete_rate_limit_policy("/api/items", "GET")
        policies = await backend.get_rate_limit_policies()
        assert policies == []

    async def test_delete_nonexistent_is_noop(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.delete_rate_limit_policy("/nonexistent", "GET")  # no raise

    async def test_multiple_policies(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        await backend.set_rate_limit_policy("/api/pay", "POST", {"limit": "5/second"})
        policies = await backend.get_rate_limit_policies()
        assert len(policies) == 2

    async def test_method_is_uppercased(self):
        from waygate.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        await backend.set_rate_limit_policy("/api/items", "get", {"limit": "10/minute"})
        await backend.delete_rate_limit_policy("/api/items", "GET")  # should work
        assert await backend.get_rate_limit_policies() == []


class TestFileBackendPolicyPersistence:
    async def test_set_persists_to_file(self, tmp_path):
        import json

        from waygate.core.backends.file import FileBackend

        path = tmp_path / "state.json"
        backend = FileBackend(path=str(path))
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        data = json.loads(path.read_text())
        assert "rl_policies" in data
        assert "GET:/api/items" in data["rl_policies"]

    async def test_loaded_on_restart(self, tmp_path):
        from waygate.core.backends.file import FileBackend

        path = tmp_path / "state.json"
        backend1 = FileBackend(path=str(path))
        await backend1.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})

        # Simulate a restart with a new instance reading the same file.
        backend2 = FileBackend(path=str(path))
        policies = await backend2.get_rate_limit_policies()
        assert len(policies) == 1
        assert policies[0]["limit"] == "10/minute"

    async def test_delete_removes_from_file(self, tmp_path):
        import json

        from waygate.core.backends.file import FileBackend

        path = tmp_path / "state.json"
        backend = FileBackend(path=str(path))
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})
        await backend.delete_rate_limit_policy("/api/items", "GET")
        data = json.loads(path.read_text())
        assert data["rl_policies"] == {}

    async def test_coexists_with_states_and_audit(self, tmp_path):
        import json

        from waygate.core.backends.file import FileBackend
        from waygate.core.models import AuditEntry, RouteState, RouteStatus

        path = tmp_path / "state.json"
        backend = FileBackend(path=str(path))
        import uuid
        from datetime import UTC, datetime

        state = RouteState(path="/api/items", status=RouteStatus.ACTIVE)
        await backend.set_state("/api/items", state)
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            path="/api/items",
            action="enable",
            previous_status=RouteStatus.MAINTENANCE,
            new_status=RouteStatus.ACTIVE,
        )
        await backend.write_audit(entry)
        await backend.set_rate_limit_policy("/api/items", "GET", {"limit": "10/minute"})

        data = json.loads(path.read_text())
        assert "states" in data
        assert "audit" in data
        assert "rl_policies" in data


# ---------------------------------------------------------------------------
# Engine-level persistence tests
# ---------------------------------------------------------------------------


class TestEngineRateLimitPersistence:
    """Engine-level rate limit persistence tests.

    All tests register the route first — ``set_rate_limit_policy`` now
    validates that the path is registered before creating a policy.
    """

    async def _register(self, engine, path: str = "/api/items") -> None:
        await engine.register(path, {"status": "active"})

    async def test_set_rate_limit_policy_registers_live(self):
        from waygate.core.engine import WaygateEngine

        engine = WaygateEngine()
        await self._register(engine)
        policy = await engine.set_rate_limit_policy("/api/items", "GET", "10/minute")
        assert policy.limit == "10/minute"
        assert "GET:/api/items" in engine._rate_limit_policies

    async def test_set_rate_limit_policy_persisted_to_backend(self):
        from waygate.core.engine import WaygateEngine

        engine = WaygateEngine()
        await self._register(engine)
        await engine.set_rate_limit_policy("/api/items", "GET", "10/minute")
        policies = await engine.backend.get_rate_limit_policies()
        assert len(policies) == 1
        assert policies[0]["limit"] == "10/minute"

    async def test_delete_rate_limit_policy_removes_from_in_memory(self):
        from waygate.core.engine import WaygateEngine

        engine = WaygateEngine()
        await self._register(engine)
        await engine.set_rate_limit_policy("/api/items", "GET", "10/minute")
        await engine.delete_rate_limit_policy("/api/items", "GET")
        assert "GET:/api/items" not in engine._rate_limit_policies

    async def test_delete_rate_limit_policy_removes_from_backend(self):
        from waygate.core.engine import WaygateEngine

        engine = WaygateEngine()
        await self._register(engine)
        await engine.set_rate_limit_policy("/api/items", "GET", "10/minute")
        await engine.delete_rate_limit_policy("/api/items", "GET")
        policies = await engine.backend.get_rate_limit_policies()
        assert policies == []

    async def test_restore_rate_limit_policies_loads_from_backend(self):
        from waygate.core.engine import WaygateEngine

        # Persist a policy directly to the backend (simulating a prior CLI call).
        engine1 = WaygateEngine()
        await self._register(engine1)
        await engine1.set_rate_limit_policy("/api/items", "GET", "10/minute")

        # Simulate a new engine instance on the same backend after a restart.
        engine2 = WaygateEngine(backend=engine1.backend)
        assert "GET:/api/items" not in engine2._rate_limit_policies  # not yet loaded

        await engine2.restore_rate_limit_policies()
        assert "GET:/api/items" in engine2._rate_limit_policies

    async def test_set_policy_with_algorithm(self):
        from waygate.core.engine import WaygateEngine
        from waygate.core.rate_limit.models import RateLimitAlgorithm

        engine = WaygateEngine()
        await self._register(engine)
        policy = await engine.set_rate_limit_policy(
            "/api/items", "GET", "10/minute", algorithm="fixed_window"
        )
        assert policy.algorithm == RateLimitAlgorithm.FIXED_WINDOW

    async def test_set_policy_with_key_strategy(self):
        from waygate.core.engine import WaygateEngine
        from waygate.core.rate_limit.models import RateLimitKeyStrategy

        engine = WaygateEngine()
        await self._register(engine)
        policy = await engine.set_rate_limit_policy(
            "/api/items", "GET", "10/minute", key_strategy="global"
        )
        assert policy.key_strategy == RateLimitKeyStrategy.GLOBAL

    async def test_set_policy_for_unknown_route_raises(self):
        """set_rate_limit_policy must reject paths that are not registered."""
        from waygate.core.engine import WaygateEngine
        from waygate.core.exceptions import RouteNotFoundException

        engine = WaygateEngine()
        with pytest.raises(RouteNotFoundException):
            await engine.set_rate_limit_policy("/does/not/exist", "GET", "10/minute")
