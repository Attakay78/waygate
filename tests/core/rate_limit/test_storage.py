"""Tests for shield.core.rate_limit.storage."""

from __future__ import annotations

import warnings

import pytest

from shield.core.exceptions import ShieldProductionWarning
from shield.core.rate_limit.models import RateLimitAlgorithm
from shield.core.rate_limit.storage import (
    HAS_LIMITS,
    FileRateLimitStorage,
    MemoryRateLimitStorage,
    create_rate_limit_storage,
)

pytestmark = pytest.mark.skipif(not HAS_LIMITS, reason="limits library not installed")


@pytest.fixture
def memory_storage():
    return MemoryRateLimitStorage()


class TestMemoryRateLimitStorage:
    async def test_increment_allows_within_limit(self, memory_storage):
        result = await memory_storage.increment(
            key="test-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is True
        assert result.remaining == 4

    async def test_increment_blocks_when_exceeded(self, memory_storage):
        # Exhaust the limit
        for _ in range(5):
            await memory_storage.increment(
                key="burst-key",
                limit="5/minute",
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            )

        result = await memory_storage.increment(
            key="burst-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is False
        assert result.remaining == 0

    async def test_different_keys_are_independent(self, memory_storage):
        for _ in range(5):
            await memory_storage.increment(
                key="key-a",
                limit="5/second",
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            )

        # key-b is untouched
        result = await memory_storage.increment(
            key="key-b",
            limit="5/second",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is True

    async def test_reset_all_for_path_clears_counter(self, memory_storage):
        for _ in range(5):
            await memory_storage.increment(
                key="reset-key",
                limit="5/minute",
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            )

        await memory_storage.reset_all_for_path("reset-key")

        result = await memory_storage.increment(
            key="reset-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is True

    async def test_get_remaining_returns_int(self, memory_storage):
        remaining = await memory_storage.get_remaining(
            key="fresh-key",
            limit="10/minute",
        )
        assert isinstance(remaining, int)
        assert remaining >= 0

    async def test_does_not_emit_production_warning(self):
        """MemoryRateLimitStorage is a single-process storage — no warning is emitted."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            MemoryRateLimitStorage()
        assert not any(issubclass(warning.category, ShieldProductionWarning) for warning in w)

    async def test_sliding_window_algorithm(self, memory_storage):
        result = await memory_storage.increment(
            key="sliding-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.SLIDING_WINDOW,
        )
        assert result.allowed is True

    async def test_moving_window_algorithm(self, memory_storage):
        result = await memory_storage.increment(
            key="moving-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.MOVING_WINDOW,
        )
        assert result.allowed is True

    async def test_token_bucket_maps_to_moving_window(self, memory_storage):
        result = await memory_storage.increment(
            key="token-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.TOKEN_BUCKET,
        )
        assert result.allowed is True

    async def test_result_has_reset_at(self, memory_storage):
        result = await memory_storage.increment(
            key="reset-at-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.reset_at is not None

    async def test_result_has_limit_string(self, memory_storage):
        result = await memory_storage.increment(
            key="limit-str-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert "10" in result.limit

    async def test_shutdown_does_not_raise(self, memory_storage):
        await memory_storage.shutdown()  # should be a no-op


class TestFileRateLimitStorage:
    async def test_emits_production_warning(self, tmp_path):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FileRateLimitStorage(
                file_path=str(tmp_path / "rl_snapshot.json"), snapshot_interval_seconds=9999
            )
        assert any(issubclass(warning.category, ShieldProductionWarning) for warning in w)

    async def test_increment_and_count(self, tmp_path):
        storage = FileRateLimitStorage(
            file_path=str(tmp_path / "rl.json"), snapshot_interval_seconds=9999
        )
        result = await storage.increment(
            key="file-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is True

    async def test_blocks_when_exceeded(self, tmp_path):
        storage = FileRateLimitStorage(
            file_path=str(tmp_path / "rl2.json"), snapshot_interval_seconds=9999
        )
        for _ in range(3):
            await storage.increment(
                key="f-key",
                limit="3/minute",
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            )
        result = await storage.increment(
            key="f-key",
            limit="3/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        assert result.allowed is False

    async def test_shutdown_does_not_raise(self, tmp_path):
        path = tmp_path / "rl_snap.json"
        storage = FileRateLimitStorage(file_path=str(path), snapshot_interval_seconds=9999)
        await storage.increment(
            key="snap-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        # shutdown() merges into existing state file; no file → no-op (no raise)
        await storage.shutdown()

    async def test_shutdown_writes_into_existing_state_file(self, tmp_path):
        import json as _json

        path = tmp_path / "state.json"
        # Pre-create a minimal state file so flush_snapshot can merge into it.
        path.write_text(_json.dumps({"states": {}, "audit": []}))
        storage = FileRateLimitStorage(file_path=str(path), snapshot_interval_seconds=9999)
        await storage.increment(
            key="snap-key",
            limit="10/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        await storage.shutdown()
        data = _json.loads(path.read_text())
        assert "rate_limits" in data

    async def test_yaml_snapshot_writes_valid_yaml(self, tmp_path):
        yaml = pytest.importorskip("yaml")
        path = tmp_path / "state.yaml"
        path.write_text("states: {}\naudit: []\n")
        storage = FileRateLimitStorage(file_path=str(path), snapshot_interval_seconds=9999)
        await storage.increment(
            key="yaml-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        await storage.shutdown()
        data = yaml.safe_load(path.read_text())
        assert "rate_limits" in data

    async def test_toml_snapshot_writes_valid_toml(self, tmp_path):
        tomli_w = pytest.importorskip("tomli_w")
        import tomllib

        path = tmp_path / "state.toml"
        path.write_bytes(tomli_w.dumps({"states": {}, "audit": []}).encode())
        storage = FileRateLimitStorage(file_path=str(path), snapshot_interval_seconds=9999)
        await storage.increment(
            key="toml-key",
            limit="5/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        await storage.shutdown()
        data = tomllib.loads(path.read_text())
        assert "rate_limits" in data


class TestCreateRateLimitStorage:
    async def test_memory_backend_returns_memory_storage(self):
        from shield.core.backends.memory import MemoryBackend

        backend = MemoryBackend()
        storage = create_rate_limit_storage(backend)
        assert isinstance(storage, MemoryRateLimitStorage)

    async def test_file_backend_returns_file_storage(self, tmp_path):
        from shield.core.backends.file import FileBackend

        backend = FileBackend(path=str(tmp_path / "state.json"))
        storage = create_rate_limit_storage(backend)
        assert isinstance(storage, FileRateLimitStorage)

    async def test_explicit_rate_limit_backend_wins(self, tmp_path):
        from shield.core.backends.file import FileBackend
        from shield.core.backends.memory import MemoryBackend

        main_backend = MemoryBackend()
        rl_backend = FileBackend(path=str(tmp_path / "state.json"))
        storage = create_rate_limit_storage(main_backend, rate_limit_backend=rl_backend)
        assert isinstance(storage, FileRateLimitStorage)
