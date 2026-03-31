"""Tests for waygate.core.config — the shared engine/backend factory."""

from __future__ import annotations

import pytest

from waygate.core.backends.file import FileBackend
from waygate.core.backends.memory import MemoryBackend
from waygate.core.config import make_backend, make_engine

# Pass config_file="" to all tests that should ignore the project .waygate file.
_NO_CFG = ""


def test_make_backend_memory_explicit():
    backend = make_backend(backend_type="memory")
    assert isinstance(backend, MemoryBackend)


def test_make_backend_memory_default(monkeypatch):
    """Without env vars or a config file the default is memory."""
    monkeypatch.delenv("WAYGATE_BACKEND", raising=False)
    backend = make_backend(config_file=_NO_CFG)
    assert isinstance(backend, MemoryBackend)


def test_make_backend_file_explicit(tmp_path):
    backend = make_backend(backend_type="file", file_path=str(tmp_path / "s.json"))
    assert isinstance(backend, FileBackend)


def test_make_backend_file_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYGATE_BACKEND", "file")
    monkeypatch.setenv("WAYGATE_FILE_PATH", str(tmp_path / "s.json"))
    backend = make_backend()
    assert isinstance(backend, FileBackend)


def test_make_backend_from_dot_waygate_file(tmp_path):
    """Values in a .waygate file are respected."""
    cfg_file = tmp_path / ".waygate"
    state_file = tmp_path / "state.json"
    cfg_file.write_text(f"WAYGATE_BACKEND=file\nWAYGATE_FILE_PATH={state_file}\n")
    backend = make_backend(config_file=str(cfg_file))
    assert isinstance(backend, FileBackend)


def test_dot_waygate_file_env_var_takes_priority(tmp_path, monkeypatch):
    """`os.environ` wins over the .waygate file."""
    cfg_file = tmp_path / ".waygate"
    cfg_file.write_text("WAYGATE_BACKEND=file\n")
    monkeypatch.setenv("WAYGATE_BACKEND", "memory")
    backend = make_backend(config_file=str(cfg_file))
    assert isinstance(backend, MemoryBackend)


def test_dot_waygate_ignores_comments(tmp_path):
    cfg_file = tmp_path / ".waygate"
    cfg_file.write_text("# this is a comment\nWAYGATE_BACKEND=memory\n")
    backend = make_backend(config_file=str(cfg_file))
    assert isinstance(backend, MemoryBackend)


def test_make_backend_unknown_raises():
    with pytest.raises(ValueError, match="Unknown WAYGATE_BACKEND"):
        make_backend(backend_type="postgres")


def test_make_engine_default_env(monkeypatch):
    monkeypatch.delenv("WAYGATE_ENV", raising=False)
    engine = make_engine(backend_type="memory")
    assert engine.current_env == "dev"


def test_make_engine_env_from_arg():
    engine = make_engine(backend_type="memory", current_env="staging")
    assert engine.current_env == "staging"


def test_make_engine_env_from_envvar(monkeypatch):
    monkeypatch.setenv("WAYGATE_ENV", "dev")
    engine = make_engine(backend_type="memory")
    assert engine.current_env == "dev"


def test_make_engine_returns_waygate_engine():
    from waygate.core.engine import WaygateEngine

    engine = make_engine(backend_type="memory")
    assert isinstance(engine, WaygateEngine)


def test_cli_and_app_use_same_file_backend(tmp_path, monkeypatch):
    """Engine built by CLI factory and app factory read/write the same file."""
    import anyio

    file_path = str(tmp_path / "shared.json")
    monkeypatch.setenv("WAYGATE_BACKEND", "file")
    monkeypatch.setenv("WAYGATE_FILE_PATH", file_path)

    async def _run():
        # Simulate app registering and then disabling a route.
        app_engine = make_engine()
        await app_engine.register("/api/pay", {"status": "active"})
        await app_engine.disable("/api/pay", reason="migration")

        # Simulate CLI reading state from the same file.
        cli_engine = make_engine()
        state = await cli_engine.get_state("/api/pay")
        assert state.status == "disabled"
        assert state.reason == "migration"

    anyio.run(_run)
