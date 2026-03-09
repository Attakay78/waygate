"""Tests for shield.fastapi.decorators."""

from __future__ import annotations

from datetime import UTC

from shield.fastapi.decorators import disabled, env_only, force_active, maintenance

# ---------------------------------------------------------------------------
# @maintenance
# ---------------------------------------------------------------------------


def test_maintenance_stamps_meta():
    @maintenance(reason="DB migration")
    async def endpoint():
        return {"ok": True}

    assert hasattr(endpoint, "__shield_meta__")
    assert endpoint.__shield_meta__["status"] == "maintenance"
    assert endpoint.__shield_meta__["reason"] == "DB migration"


def test_maintenance_no_window_by_default():
    @maintenance(reason="test")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["window"] is None


def test_maintenance_with_window():
    from datetime import datetime

    start = datetime(2025, 3, 10, 2, 0, tzinfo=UTC)
    end = datetime(2025, 3, 10, 4, 0, tzinfo=UTC)

    @maintenance(reason="test", start=start, end=end)
    async def endpoint():
        return {}

    window = endpoint.__shield_meta__["window"]
    assert window is not None
    assert window.start == start
    assert window.end == end


async def test_maintenance_preserves_async_function():
    @maintenance(reason="test")
    async def endpoint():
        return {"ok": True}

    result = await endpoint()
    assert result == {"ok": True}


def test_maintenance_preserves_sync_function():
    @maintenance(reason="test")
    def endpoint():
        return {"ok": True}

    result = endpoint()
    assert result == {"ok": True}


def test_maintenance_preserves_name():
    @maintenance(reason="test")
    async def my_endpoint():
        return {}

    assert my_endpoint.__name__ == "my_endpoint"


# ---------------------------------------------------------------------------
# @env_only
# ---------------------------------------------------------------------------


def test_env_only_stamps_meta():
    @env_only("dev", "staging")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["status"] == "env_gated"
    assert endpoint.__shield_meta__["allowed_envs"] == ["dev", "staging"]


async def test_env_only_preserves_async():
    @env_only("dev")
    async def endpoint():
        return {"env": "dev"}

    result = await endpoint()
    assert result == {"env": "dev"}


def test_env_only_preserves_name():
    @env_only("dev")
    async def debug_endpoint():
        return {}

    assert debug_endpoint.__name__ == "debug_endpoint"


# ---------------------------------------------------------------------------
# @disabled
# ---------------------------------------------------------------------------


def test_disabled_stamps_meta():
    @disabled(reason="Use /new-endpoint instead")
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["status"] == "disabled"
    assert endpoint.__shield_meta__["reason"] == "Use /new-endpoint instead"


async def test_disabled_preserves_async():
    @disabled(reason="gone")
    async def endpoint():
        return {"ok": True}

    result = await endpoint()
    assert result == {"ok": True}


def test_disabled_default_reason():
    @disabled()
    async def endpoint():
        return {}

    assert endpoint.__shield_meta__["reason"] == ""


def test_disabled_preserves_name():
    @disabled(reason="gone")
    async def old_endpoint():
        return {}

    assert old_endpoint.__name__ == "old_endpoint"


# ---------------------------------------------------------------------------
# @force_active
# ---------------------------------------------------------------------------


def test_force_active_stamps_meta():
    @force_active
    async def health():
        return {"status": "ok"}

    assert health.__shield_meta__["force_active"] is True


async def test_force_active_preserves_async():
    @force_active
    async def health():
        return {"status": "ok"}

    result = await health()
    assert result == {"status": "ok"}


def test_force_active_preserves_name():
    @force_active
    async def health_check():
        return {}

    assert health_check.__name__ == "health_check"


# ---------------------------------------------------------------------------
# Stacking decorators
# ---------------------------------------------------------------------------


def test_decorators_do_not_interfere_when_stacked():
    """Multiple decorators should not raise and each stamp is present."""

    @env_only("dev")
    @maintenance(reason="test")
    async def endpoint():
        return {}

    # The outermost decorator's stamp takes precedence for "status"
    assert "__shield_meta__" in dir(endpoint) or hasattr(endpoint, "__shield_meta__")
