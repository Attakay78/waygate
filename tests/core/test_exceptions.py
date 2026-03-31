"""Tests for waygate.core.exceptions."""

from datetime import UTC, datetime

from waygate.core.exceptions import (
    EnvGatedException,
    MaintenanceException,
    RouteDisabledException,
    WaygateException,
)


def test_waygate_exception_is_base():
    exc = WaygateException("test")
    assert isinstance(exc, Exception)


def test_maintenance_exception_attrs():
    retry = datetime(2025, 3, 10, 4, 0, 0, tzinfo=UTC)
    exc = MaintenanceException(reason="DB migration", retry_after=retry)
    assert exc.reason == "DB migration"
    assert exc.retry_after == retry
    assert isinstance(exc, WaygateException)


def test_maintenance_exception_defaults():
    exc = MaintenanceException()
    assert exc.reason == ""
    assert exc.retry_after is None


def test_env_gated_exception_attrs():
    exc = EnvGatedException(
        path="/api/debug", current_env="production", allowed_envs=["dev", "staging"]
    )
    assert exc.path == "/api/debug"
    assert exc.current_env == "production"
    assert exc.allowed_envs == ["dev", "staging"]
    assert isinstance(exc, WaygateException)
    assert "production" in str(exc)


def test_route_disabled_exception_attrs():
    exc = RouteDisabledException(reason="Use /new-endpoint instead")
    assert exc.reason == "Use /new-endpoint instead"
    assert isinstance(exc, WaygateException)


def test_route_disabled_exception_default():
    exc = RouteDisabledException()
    assert exc.reason == ""
