"""Tests for the shield CLI — HTTP client mode.

The CLI is now a thin HTTP client.  Tests create an in-process ShieldAdmin
ASGI app and inject it into the CLI via the ``make_client`` monkeypatch,
so no real server is needed.

IMPORTANT: Tests that call ``invoke_with_client`` must be sync (``def``, not
``async def``) because the CLI uses ``anyio.run()`` internally and that
cannot be nested inside a running pytest-asyncio event loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import anyio
import httpx
import pytest
import pytest as _pytest
from typer.testing import CliRunner

from shield.admin.app import ShieldAdmin
from shield.cli.client import ShieldClient, ShieldClientError
from shield.cli.main import _parse_dt, _parse_route, _parse_until
from shield.cli.main import cli as app
from shield.core.engine import ShieldEngine
from shield.core.models import RouteState, RouteStatus
from shield.core.rate_limit.storage import HAS_LIMITS

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_engine(*paths: str) -> ShieldEngine:
    """Create a ShieldEngine and seed *paths* as ACTIVE routes (synchronously)."""
    e = ShieldEngine()

    async def _run() -> None:
        for path in paths:
            await e.backend.set_state(path, RouteState(path=path, status=RouteStatus.ACTIVE))

    anyio.run(_run)
    return e


def _do_async(coro_fn: object) -> object:
    """Run a no-argument async callable synchronously and return the result."""
    results: list[object] = []

    async def _wrap() -> None:
        results.append(await coro_fn())  # type: ignore[operator]

    anyio.run(_wrap)
    return results[0] if results else None


def _open_client(engine: ShieldEngine) -> ShieldClient:
    """Return a ShieldClient backed by an in-process ShieldAdmin (no auth)."""
    admin = ShieldAdmin(engine=engine)
    return ShieldClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=admin),  # type: ignore[arg-type]
    )


def invoke_with_client(client: ShieldClient, *args: str) -> object:
    """Invoke a CLI command with *client* injected via ``make_client``."""
    with patch("shield.cli.main.make_client", return_value=client):
        return runner.invoke(app, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Helper function unit tests (pure Python — no server needed)
# ---------------------------------------------------------------------------


def test_parse_until_hours() -> None:
    dt = _parse_until("2h")
    now = datetime.now(UTC)
    diff = (dt - now).total_seconds()
    assert 7180 < diff < 7220


def test_parse_until_minutes() -> None:
    dt = _parse_until("30m")
    now = datetime.now(UTC)
    diff = (dt - now).total_seconds()
    assert 1790 < diff < 1820


def test_parse_until_days() -> None:
    dt = _parse_until("1d")
    now = datetime.now(UTC)
    diff = (dt - now).total_seconds()
    assert 86380 < diff < 86420


def test_parse_until_bad_unit() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_until("2w")


def test_parse_dt_z_suffix() -> None:
    dt = _parse_dt("2025-03-10T02:00Z")
    assert dt.year == 2025
    assert dt.hour == 2
    assert dt.tzinfo is not None


def test_parse_dt_iso_no_tz() -> None:
    dt = _parse_dt("2025-03-10T02:00:00")
    assert dt.tzinfo is not None


def test_parse_route_path_only() -> None:
    assert _parse_route("/payments") == "/payments"


def test_parse_route_method_path() -> None:
    assert _parse_route("GET:/payments") == "GET:/payments"


def test_parse_route_bad_method() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_route("BREW:/payments")


def test_parse_route_no_leading_slash() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_route("payments")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_empty() -> None:
    e = ShieldEngine()  # empty — no need for async seeding
    client = _open_client(e)
    result = invoke_with_client(client, "status")
    assert result.exit_code == 0
    assert "No routes" in result.output


def test_status_shows_registered_routes() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    result = invoke_with_client(client, "status")
    assert result.exit_code == 0
    assert "/api/p" in result.output  # Rich may truncate path in narrow terminal
    assert "ACTIVE" in result.output


def test_status_shows_disabled_route() -> None:
    e = _seed_engine("/api/pay")
    _do_async(lambda: e.disable("/api/pay", reason="test"))
    client = _open_client(e)
    result = invoke_with_client(client, "status")
    assert result.exit_code == 0
    assert "DISABLED" in result.output


# ---------------------------------------------------------------------------
# enable / disable round-trip
# ---------------------------------------------------------------------------


def test_enable_disable_round_trip() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)

    result = invoke_with_client(client, "disable", "/api/pay", "--reason", "migration")
    assert result.exit_code == 0
    assert "DISABLED" in result.output

    result = invoke_with_client(client, "status")
    assert "DISABLED" in result.output

    result = invoke_with_client(client, "enable", "/api/pay")
    assert result.exit_code == 0
    assert "ACTIVE" in result.output

    result = invoke_with_client(client, "status")
    assert "ACTIVE" in result.output


def test_disable_shows_reason() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    result = invoke_with_client(client, "disable", "/api/pay", "--reason", "security incident")
    assert result.exit_code == 0
    assert "security incident" in result.output


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------


def test_maintenance_command() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    result = invoke_with_client(client, "maintenance", "/api/pay", "--reason", "DB swap")
    assert result.exit_code == 0
    assert "MAINTENANCE" in result.output

    state = _do_async(lambda: e.get_state("/api/pay"))
    assert state.status == RouteStatus.MAINTENANCE  # type: ignore[union-attr]


def test_maintenance_with_window() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    result = invoke_with_client(
        client,
        "maintenance",
        "/api/pay",
        "--reason",
        "DB swap",
        "--start",
        "2025-03-10T02:00Z",
        "--end",
        "2025-03-10T04:00Z",
    )
    assert result.exit_code == 0
    assert "MAINTENANCE" in result.output


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


def test_schedule_command() -> None:
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    start = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%MZ")
    end = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
    result = invoke_with_client(
        client,
        "schedule",
        "/api/pay",
        "--start",
        start,
        "--end",
        end,
        "--reason",
        "nightly migration",
    )
    assert result.exit_code == 0
    assert "Scheduled" in result.output
    assert "nightly migration" in result.output


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def test_log_empty() -> None:
    e = ShieldEngine()
    client = _open_client(e)
    result = invoke_with_client(client, "log")
    assert result.exit_code == 0
    assert "No audit entries" in result.output


def test_log_shows_entries() -> None:
    e = _seed_engine("/api/pay")
    _do_async(lambda: e.disable("/api/pay", reason="gone", actor="tester"))
    _do_async(lambda: e.enable("/api/pay", actor="tester"))
    client = _open_client(e)
    result = invoke_with_client(client, "log")
    assert result.exit_code == 0
    assert "api" in result.output  # path cell (may be truncated in narrow terminal)
    assert "disable" in result.output
    assert "enable" in result.output


def test_log_filter_by_route() -> None:
    e = _seed_engine("/api/pay", "/api/users")
    _do_async(lambda: e.disable("/api/pay", reason="p"))
    _do_async(lambda: e.disable("/api/users", reason="u"))
    client = _open_client(e)
    result = invoke_with_client(client, "log", "--route", "/api/pay")
    assert result.exit_code == 0
    assert "api" in result.output


def test_log_limit() -> None:
    e = _seed_engine("/api/pay")
    for _ in range(5):
        _do_async(lambda: e.disable("/api/pay", reason="x"))
        _do_async(lambda: e.enable("/api/pay"))
    client = _open_client(e)
    result = invoke_with_client(client, "log", "--limit", "3")
    assert result.exit_code == 0
    assert result.output.count("/api/pay") <= 3


# ---------------------------------------------------------------------------
# config commands (no server needed — reads / writes config file)
# ---------------------------------------------------------------------------


# -- URL auto-discovery ------------------------------------------------------


def test_get_server_url_from_env(monkeypatch) -> None:
    """SHIELD_SERVER_URL env var is the highest priority source."""
    monkeypatch.setenv("SHIELD_SERVER_URL", "http://env-host:9000/shield")
    from shield.cli import config as cfg

    assert cfg.get_server_url() == "http://env-host:9000/shield"


def test_get_server_url_from_dot_shield_file(tmp_path, monkeypatch) -> None:
    """.shield file SERVER_URL is used when no env var is set."""
    shield_file = tmp_path / ".shield"
    shield_file.write_text("SHIELD_SERVER_URL=http://project-host:8080/shield\n")
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    # Point find_shield_file at our tmp directory.
    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: shield_file)
    assert cfg.get_server_url() == "http://project-host:8080/shield"


def test_get_server_url_from_user_config(tmp_path, monkeypatch) -> None:
    """~/.shield/config.json is used when no env var and no .shield file."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"server_url": "http://user-config-host/shield"}')
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: None)
    monkeypatch.setattr(cfg, "get_config_path", lambda: config_file)
    assert cfg.get_server_url() == "http://user-config-host/shield"


def test_get_server_url_default(monkeypatch) -> None:
    """Falls back to the built-in default when nothing is configured."""
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: None)
    monkeypatch.setattr(cfg, "get_config_path", lambda: cfg.Path("/nonexistent/config.json"))
    assert cfg.get_server_url() == "http://localhost:8000/shield"


def test_env_var_takes_priority_over_dot_shield(tmp_path, monkeypatch) -> None:
    """Env var wins over .shield file."""
    shield_file = tmp_path / ".shield"
    shield_file.write_text("SHIELD_SERVER_URL=http://project-host/shield\n")
    monkeypatch.setenv("SHIELD_SERVER_URL", "http://env-wins/shield")
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: shield_file)
    assert cfg.get_server_url() == "http://env-wins/shield"


def test_dot_shield_takes_priority_over_user_config(tmp_path, monkeypatch) -> None:
    """.shield file wins over user config.json."""
    shield_file = tmp_path / ".shield"
    shield_file.write_text("SHIELD_SERVER_URL=http://project-wins/shield\n")
    config_file = tmp_path / "config.json"
    config_file.write_text('{"server_url": "http://user-config/shield"}')
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: shield_file)
    monkeypatch.setattr(cfg, "get_config_path", lambda: config_file)
    assert cfg.get_server_url() == "http://project-wins/shield"


def test_find_shield_file_walks_up(tmp_path) -> None:
    """find_shield_file walks up from a subdirectory."""
    from shield.cli.config import find_shield_file

    # Create .shield in tmp_path and start search from a nested subdir.
    shield_file = tmp_path / ".shield"
    shield_file.write_text("SHIELD_SERVER_URL=http://found/shield\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = find_shield_file(start=nested)
    assert found == shield_file


def test_find_shield_file_returns_none_when_absent(tmp_path) -> None:
    """find_shield_file returns None when no .shield file exists in the tree."""
    from shield.cli.config import find_shield_file

    # Use a temp dir with no .shield file.
    nested = tmp_path / "deep"
    nested.mkdir()
    # We can't easily test the full walk without a .shield file, but we
    # can verify a path that definitely won't have one (the tmp dir itself).
    assert find_shield_file(start=tmp_path) is None


def test_require_server_url_always_returns_value(monkeypatch) -> None:
    """require_server_url() always returns a string (no SystemExit)."""
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: None)
    monkeypatch.setattr(cfg, "get_config_path", lambda: cfg.Path("/nonexistent/config.json"))
    url = cfg.require_server_url()
    assert isinstance(url, str)
    assert url  # non-empty


def test_url_source_env(monkeypatch) -> None:
    monkeypatch.setenv("SHIELD_SERVER_URL", "http://env/shield")
    from shield.cli import config as cfg

    assert "env" in cfg.get_server_url_source()


def test_url_source_default(monkeypatch) -> None:
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: None)
    monkeypatch.setattr(cfg, "get_config_path", lambda: cfg.Path("/nonexistent/config.json"))
    assert cfg.get_server_url_source() == "default"


def test_config_show_displays_source(tmp_path, monkeypatch) -> None:
    """config show includes the URL source in parentheses."""
    monkeypatch.delenv("SHIELD_SERVER_URL", raising=False)
    from shield.cli import config as cfg

    monkeypatch.setattr(cfg, "find_shield_file", lambda start=None: None)
    monkeypatch.setattr(
        "shield.cli.config.get_config_path",
        lambda: tmp_path / "config.json",
    )
    result = runner.invoke(app, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Server URL" in result.output
    assert "default" in result.output


def test_config_set_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shield.cli.config.get_config_path",
        lambda: tmp_path / "config.json",
    )
    result = runner.invoke(
        app,
        ["config", "set-url", "http://localhost:8000/shield"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "localhost:8000" in result.output


def test_config_show(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shield.cli.config.get_config_path",
        lambda: tmp_path / "config.json",
    )
    result = runner.invoke(app, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Server URL" in result.output


# ---------------------------------------------------------------------------
# auth — login / logout
# ---------------------------------------------------------------------------


def test_login_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shield.cli.config.get_config_path",
        lambda: tmp_path / "config.json",
    )
    e = ShieldEngine()
    admin = ShieldAdmin(engine=e, auth=("admin", "secret"))
    transport = httpx.ASGITransport(app=admin)  # type: ignore[arg-type]

    with (
        patch("shield.cli.config.require_server_url", return_value="http://testserver"),
        patch(
            "shield.cli.main.ShieldClient",
            return_value=ShieldClient(base_url="http://testserver", transport=transport),
        ),
    ):
        result = runner.invoke(
            app,
            ["login", "admin", "--password", "secret"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "Logged in" in result.output
    assert "admin" in result.output


def test_login_wrong_password_exits_1(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shield.cli.config.get_config_path",
        lambda: tmp_path / "config.json",
    )
    e = ShieldEngine()
    admin = ShieldAdmin(engine=e, auth=("admin", "secret"))
    transport = httpx.ASGITransport(app=admin)  # type: ignore[arg-type]

    with (
        patch("shield.cli.config.require_server_url", return_value="http://testserver"),
        patch(
            "shield.cli.main.ShieldClient",
            return_value=ShieldClient(base_url="http://testserver", transport=transport),
        ),
    ):
        result = runner.invoke(
            app,
            ["login", "admin", "--password", "wrong"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 401 handling
# ---------------------------------------------------------------------------


def test_401_shows_login_hint() -> None:
    e = ShieldEngine()
    client = _open_client(e)
    with patch.object(client, "list_routes", side_effect=ShieldClientError("Auth required", 401)):
        result = invoke_with_client(client, "status")
    assert result.exit_code == 1
    assert "login" in result.output.lower()


# ---------------------------------------------------------------------------
# global commands
# ---------------------------------------------------------------------------


def test_global_status() -> None:
    e = ShieldEngine()
    client = _open_client(e)
    result = invoke_with_client(client, "global", "status")
    assert result.exit_code == 0
    assert "Global maintenance" in result.output


def test_global_enable_disable() -> None:
    e = ShieldEngine()
    client = _open_client(e)

    result = invoke_with_client(client, "global", "enable", "--reason", "emergency")
    assert result.exit_code == 0
    assert "ENABLED" in result.output

    result = invoke_with_client(client, "global", "disable")
    assert result.exit_code == 0
    assert "DISABLED" in result.output


# ---------------------------------------------------------------------------
# Method-prefixed route keys
# ---------------------------------------------------------------------------


def test_disable_method_specific_route() -> None:
    e = _seed_engine("GET:/payments", "POST:/payments")
    client = _open_client(e)

    result = invoke_with_client(client, "disable", "GET:/payments", "--reason", "read-only")
    assert result.exit_code == 0
    assert "DISABLED" in result.output
    assert "GET:" in result.output


def test_invalid_method_raises_error() -> None:
    e = ShieldEngine()
    _open_client(e)
    result = runner.invoke(app, ["disable", "BREW:/payments"], catch_exceptions=False)
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Rate limit CLI commands
# ---------------------------------------------------------------------------

_rl_skipif = _pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


@_rl_skipif
def test_rl_set_creates_policy() -> None:
    """shield rl set GET:/api/items 10/minute — METHOD:/path form."""
    e = _seed_engine("/api/items")
    client = _open_client(e)
    result = invoke_with_client(client, "rl", "set", "GET:/api/items", "10/minute")
    assert result.exit_code == 0, result.output
    assert "10/minute" in result.output


@_rl_skipif
def test_rl_set_path_only_defaults_to_get() -> None:
    """shield rl set /api/items 10/minute — plain /path defaults to GET."""
    e = _seed_engine("/api/items")
    client = _open_client(e)
    result = invoke_with_client(client, "rl", "set", "/api/items", "10/minute")
    assert result.exit_code == 0, result.output
    assert "10/minute" in result.output


@_rl_skipif
def test_rl_set_with_method_and_algorithm() -> None:
    """shield rl set POST:/api/pay 5/second --algorithm fixed_window."""
    e = _seed_engine("/api/pay")
    client = _open_client(e)
    result = invoke_with_client(
        client,
        "rl",
        "set",
        "POST:/api/pay",
        "5/second",
        "--algorithm",
        "fixed_window",
    )
    assert result.exit_code == 0, result.output
    assert "fixed_window" in result.output


@_rl_skipif
def test_rl_list_shows_policies() -> None:
    e = _seed_engine("/api/items")
    client = _open_client(e)
    invoke_with_client(client, "rl", "set", "GET:/api/items", "100/hour")
    result = invoke_with_client(client, "rate-limits", "list")  # both aliases work
    assert result.exit_code == 0, result.output
    assert "api" in result.output  # /api/items path shown


@_rl_skipif
def test_rl_delete_removes_policy() -> None:
    """shield rl delete GET:/api/items — METHOD:/path form."""
    e = _seed_engine("/api/items")
    client = _open_client(e)
    invoke_with_client(client, "rl", "set", "GET:/api/items", "10/minute")
    result = invoke_with_client(client, "rl", "delete", "GET:/api/items")
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output.lower()

    policies = _do_async(lambda: e.backend.get_rate_limit_policies())
    assert policies == []


@_rl_skipif
def test_rl_delete_path_only_defaults_to_get() -> None:
    """shield rl delete /api/items — plain /path defaults to GET."""
    e = _seed_engine("/api/items")
    client = _open_client(e)
    invoke_with_client(client, "rl", "set", "GET:/api/items", "10/minute")
    result = invoke_with_client(client, "rl", "delete", "/api/items")
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output.lower()
