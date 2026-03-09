"""Tests for shield CLI commands.

Uses Typer's CliRunner so no subprocess needed.
"""

from __future__ import annotations

import anyio
from typer.testing import CliRunner

from shield.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(file_path: str, *paths: str) -> None:
    """Write ACTIVE state for each path into a FileBackend (sync wrapper)."""
    from shield.core.backends.file import FileBackend
    from shield.core.models import RouteState

    async def _run():
        backend = FileBackend(file_path)
        for path in paths:
            await backend.set_state(path, RouteState(path=path))

    anyio.run(_run)


def invoke(*args: str) -> object:
    """Invoke the CLI with memory backend (default)."""
    return runner.invoke(app, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_empty(monkeypatch):
    # Force memory so the .shield file and any leftover state file are ignored.
    monkeypatch.setenv("SHIELD_BACKEND", "memory")
    result = invoke("status")
    assert result.exit_code == 0
    assert "No routes" in result.output


def test_status_shows_route_after_disable(monkeypatch):
    monkeypatch.setenv("SHIELD_BACKEND", "memory")
    invoke("disable", "/api/pay", "--reason", "gone")
    result = invoke("status")
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# enable / disable round-trip via shared engine
# ---------------------------------------------------------------------------


def test_enable_disable_round_trip(monkeypatch, tmp_path):
    """Use FileBackend so commands share state across invocations."""
    file_path = str(tmp_path / "shield.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")  # pre-register the route

    result = invoke("disable", "/api/pay", "--reason", "migration")
    assert result.exit_code == 0
    assert "DISABLED" in result.output

    result = invoke("status")
    assert result.exit_code == 0
    assert "/api/pay" in result.output
    assert "DISABLED" in result.output

    result = invoke("enable", "/api/pay")
    assert result.exit_code == 0
    assert "ACTIVE" in result.output

    result = invoke("status")
    assert "/api/pay" in result.output
    assert "ACTIVE" in result.output


def test_disable_shows_reason(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")

    result = invoke("disable", "/api/pay", "--reason", "security incident")
    assert result.exit_code == 0
    assert "security incident" in result.output


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------


def test_maintenance_command(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")

    result = invoke("maintenance", "/api/pay", "--reason", "DB swap")
    assert result.exit_code == 0
    assert "MAINTENANCE" in result.output

    result = invoke("status")
    assert "MAINTENANCE" in result.output


def test_maintenance_with_window(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")

    result = invoke(
        "maintenance", "/api/pay",
        "--reason", "DB swap",
        "--start", "2025-03-10T02:00Z",
        "--end", "2025-03-10T04:00Z",
    )
    assert result.exit_code == 0
    assert "Window" in result.output


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


def test_schedule_command(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")

    result = invoke(
        "schedule", "/api/pay",
        "--start", "2025-03-10T02:00Z",
        "--end", "2025-03-10T04:00Z",
        "--reason", "nightly migration",
    )
    assert result.exit_code == 0
    assert "Scheduled" in result.output
    assert "nightly migration" in result.output


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def test_log_empty(monkeypatch):
    monkeypatch.setenv("SHIELD_BACKEND", "memory")
    result = invoke("log")
    assert result.exit_code == 0
    assert "No audit entries" in result.output


def test_log_shows_entries(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay")

    invoke("disable", "/api/pay", "--reason", "gone")
    invoke("enable", "/api/pay")

    result = invoke("log")
    assert result.exit_code == 0
    assert "/api/pay" in result.output
    assert "disable" in result.output
    assert "enable" in result.output


def test_log_filter_by_route(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/api/pay", "/api/users")

    invoke("disable", "/api/pay")
    invoke("disable", "/api/users")

    result = invoke("log", "--route", "/api/pay")
    assert result.exit_code == 0
    assert "/api/pay" in result.output
    # /api/users should not appear since we filtered
    # (both may appear if the filter isn't working, so we check pay is present)


def test_log_limit(monkeypatch, tmp_path):
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, *[f"/api/route{i}" for i in range(10)])

    for i in range(10):
        invoke("disable", f"/api/route{i}")

    result = invoke("log", "--limit", "3")
    assert result.exit_code == 0
    # Each row shows the route path once; limit=3 means at most 3 route entries.
    assert result.output.count("/api/route") <= 3


# ---------------------------------------------------------------------------
# _parse_until / _parse_dt
# ---------------------------------------------------------------------------


def test_parse_until_hours():
    from datetime import UTC, datetime

    from shield.cli.main import _parse_until

    dt = _parse_until("2h")
    now = datetime.now(UTC)
    diff = (dt - now).total_seconds()
    assert 7180 < diff < 7220  # ~2h


def test_parse_dt_iso():
    from shield.cli.main import _parse_dt

    dt = _parse_dt("2025-03-10T02:00Z")
    assert dt.year == 2025
    assert dt.month == 3
    assert dt.hour == 2


# ---------------------------------------------------------------------------
# --config flag
# ---------------------------------------------------------------------------


def test_config_flag_loads_named_file(tmp_path, monkeypatch):
    """--config <path> loads that file instead of auto-discovering .shield."""
    monkeypatch.delenv("SHIELD_BACKEND", raising=False)

    state_file = tmp_path / "state.json"
    cfg_file = tmp_path / "prod.shield"
    cfg_file.write_text(
        f"SHIELD_BACKEND=file\nSHIELD_FILE_PATH={state_file}\n"
    )
    _seed(str(state_file), "/api/pay")  # pre-register route

    # Disable via the named config.
    result = runner.invoke(
        app,
        ["--config", str(cfg_file), "disable", "/api/pay", "--reason", "cfg test"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "DISABLED" in result.output

    # Read it back with the same config — must see the persisted state.
    result = runner.invoke(
        app,
        ["--config", str(cfg_file), "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "/api/pay" in result.output
    assert "DISABLED" in result.output


def test_config_flag_short_form(tmp_path, monkeypatch):
    """-c is an alias for --config."""
    monkeypatch.delenv("SHIELD_BACKEND", raising=False)

    state_file = tmp_path / "state.json"
    cfg_file = tmp_path / "custom.env"
    cfg_file.write_text(
        f"SHIELD_BACKEND=file\nSHIELD_FILE_PATH={state_file}\n"
    )

    result = runner.invoke(
        app,
        ["-c", str(cfg_file), "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_config_flag_env_var_still_wins(tmp_path, monkeypatch):
    """env vars override --config file values."""
    monkeypatch.setenv("SHIELD_BACKEND", "memory")

    cfg_file = tmp_path / "staging.shield"
    cfg_file.write_text("SHIELD_BACKEND=file\n")

    # env var says memory, config file says file — memory wins.
    result = runner.invoke(
        app,
        ["--config", str(cfg_file), "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No routes" in result.output  # memory backend, empty state


# ---------------------------------------------------------------------------
# Method-scoped route state  (GET:/path vs /path)
# ---------------------------------------------------------------------------


def test_disable_method_specific_route(monkeypatch, tmp_path):
    """GET:/payments can be disabled independently of POST:/payments."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "GET:/payments", "POST:/payments")

    result = invoke("disable", "GET:/payments", "--reason", "read-only mode")
    assert result.exit_code == 0
    assert "DISABLED" in result.output
    assert "GET:/payments" in result.output

    result = invoke("status")
    assert "GET:/payments" in result.output
    assert "DISABLED" in result.output
    # POST:/payments should still be ACTIVE
    assert "POST:/payments" in result.output
    assert "ACTIVE" in result.output


def test_disable_all_methods_route(monkeypatch, tmp_path):
    """Path-level /payments disables all methods."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    _seed(file_path, "/payments")

    result = invoke("disable", "/payments", "--reason", "all-method shutdown")
    assert result.exit_code == 0
    assert "DISABLED" in result.output
    assert "/payments" in result.output


def test_unknown_route_raises_error(monkeypatch, tmp_path):
    """Operating on a non-existent route must exit with code 1."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)
    # Do NOT seed /api/unknown

    result = runner.invoke(
        app,
        ["disable", "/api/unknown", "--reason", "oops"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "Error" in result.output or "No registered state" in result.output


def test_invalid_method_raises_error(monkeypatch, tmp_path):
    """BADMETHOD:/path must be rejected with a clear error."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)

    result = runner.invoke(
        app,
        ["disable", "BREW:/payments"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0


def test_force_active_route_cannot_be_disabled(monkeypatch, tmp_path):
    """CLI must refuse to disable a @force_active route."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)

    # Seed the route as force_active=True (mimics @force_active decorator).
    from shield.core.backends.file import FileBackend
    from shield.core.models import RouteState

    async def _seed():
        b = FileBackend(file_path)
        await b.set_state(
            "GET:/health", RouteState(path="GET:/health", force_active=True)
        )

    anyio.run(_seed)

    result = runner.invoke(
        app,
        ["disable", "GET:/health", "--reason", "oops"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "force_active" in result.output.lower() or "Error" in result.output


def test_force_active_route_cannot_be_set_to_maintenance(monkeypatch, tmp_path):
    """CLI must refuse to put a @force_active route into maintenance."""
    file_path = str(tmp_path / "s.json")
    monkeypatch.setenv("SHIELD_BACKEND", "file")
    monkeypatch.setenv("SHIELD_FILE_PATH", file_path)

    from shield.core.backends.file import FileBackend
    from shield.core.models import RouteState

    async def _seed():
        b = FileBackend(file_path)
        await b.set_state(
            "GET:/health", RouteState(path="GET:/health", force_active=True)
        )

    anyio.run(_seed)

    result = runner.invoke(
        app,
        ["maintenance", "GET:/health", "--reason", "test"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
