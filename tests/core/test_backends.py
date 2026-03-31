"""Parametrized backend tests — the same suite runs against every backend."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waygate.core.backends.file import FileBackend
from waygate.core.backends.memory import MemoryBackend
from waygate.core.backends.redis import RedisBackend
from waygate.core.models import AuditEntry, RouteState, RouteStatus

# ---------------------------------------------------------------------------
# Redis availability check
# ---------------------------------------------------------------------------

REDIS_URL = "redis://localhost:6379/15"  # DB 15 for tests


async def _redis_available() -> bool:
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=1)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_backend() -> MemoryBackend:
    return MemoryBackend()


@pytest.fixture
def file_backend(tmp_path: Path) -> FileBackend:
    return FileBackend(str(tmp_path / "waygate.json"))


@pytest.fixture
async def redis_backend():
    if not await _redis_available():
        pytest.skip("Redis not available")
    backend = RedisBackend(url=REDIS_URL)
    # Flush the test DB before each test for isolation.
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_URL)
    await r.flushdb()
    await r.aclose()
    yield backend


@pytest.fixture(params=["memory", "file", "redis"])
def backend(request, memory_backend, file_backend, redis_backend):
    if request.param == "memory":
        return memory_backend
    if request.param == "file":
        return file_backend
    return redis_backend


def _make_state(path: str = "/api/test", status: RouteStatus = RouteStatus.ACTIVE) -> RouteState:
    return RouteState(path=path, status=status, reason="test")


def _make_audit(path: str = "/api/test") -> AuditEntry:
    return AuditEntry(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        path=path,
        action="enable",
        previous_status=RouteStatus.MAINTENANCE,
        new_status=RouteStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Shared suite
# ---------------------------------------------------------------------------


async def test_set_and_get_state(backend):
    state = _make_state()
    await backend.set_state("/api/test", state)
    result = await backend.get_state("/api/test")
    assert result.path == "/api/test"
    assert result.status == RouteStatus.ACTIVE


async def test_get_state_missing_raises_key_error(backend):
    with pytest.raises(KeyError):
        await backend.get_state("/not/registered")


async def test_set_state_overwrites(backend):
    await backend.set_state("/api/test", _make_state(status=RouteStatus.ACTIVE))
    await backend.set_state("/api/test", _make_state(status=RouteStatus.DISABLED))
    result = await backend.get_state("/api/test")
    assert result.status == RouteStatus.DISABLED


async def test_delete_state(backend):
    await backend.set_state("/api/test", _make_state())
    await backend.delete_state("/api/test")
    with pytest.raises(KeyError):
        await backend.get_state("/api/test")


async def test_delete_state_noop_if_missing(backend):
    """delete_state on an unregistered path must not raise."""
    await backend.delete_state("/not/registered")


async def test_list_states_empty(backend):
    states = await backend.list_states()
    assert states == []


async def test_list_states(backend):
    await backend.set_state("/api/a", _make_state("/api/a"))
    await backend.set_state("/api/b", _make_state("/api/b"))
    states = await backend.list_states()
    paths = {s.path for s in states}
    assert paths == {"/api/a", "/api/b"}


async def test_write_and_read_audit(backend):
    entry = _make_audit()
    await backend.write_audit(entry)
    log = await backend.get_audit_log()
    assert len(log) == 1
    assert log[0].id == entry.id


async def test_audit_log_newest_first(backend):
    e1 = _make_audit()
    e2 = _make_audit()
    await backend.write_audit(e1)
    await backend.write_audit(e2)
    log = await backend.get_audit_log()
    assert log[0].id == e2.id  # newest first


async def test_audit_log_filter_by_path(backend):
    e_pay = _make_audit("/api/payments")
    e_usr = _make_audit("/api/users")
    await backend.write_audit(e_pay)
    await backend.write_audit(e_usr)
    log = await backend.get_audit_log(path="/api/payments")
    assert all(e.path == "/api/payments" for e in log)
    assert len(log) == 1


async def test_audit_log_limit(backend):
    for _ in range(10):
        await backend.write_audit(_make_audit())
    log = await backend.get_audit_log(limit=5)
    assert len(log) == 5


async def test_audit_cap_at_1000(backend):
    """Audit log must not grow beyond 1000 entries."""
    for _ in range(1005):
        await backend.write_audit(_make_audit())
    log = await backend.get_audit_log(limit=2000)
    assert len(log) <= 1000


# ---------------------------------------------------------------------------
# MemoryBackend-specific: subscribe()
# ---------------------------------------------------------------------------


async def test_memory_subscribe():
    backend = MemoryBackend()
    received: list[RouteState] = []

    async def _collect():
        async for state in backend.subscribe():
            received.append(state)
            break  # collect exactly one then stop

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)  # yield so task starts
    state = _make_state(status=RouteStatus.DISABLED)
    await backend.set_state("/api/test", state)
    await task

    assert len(received) == 1
    assert received[0].status == RouteStatus.DISABLED


# ---------------------------------------------------------------------------
# FileBackend-specific: subscribe() raises NotImplementedError
# ---------------------------------------------------------------------------


async def test_file_subscribe_raises(tmp_path):
    backend = FileBackend(str(tmp_path / "s.json"))
    with pytest.raises(NotImplementedError):
        async for _ in backend.subscribe():
            pass


async def test_file_backend_persists_between_instances(tmp_path):
    """Data written by one FileBackend instance is readable by another."""
    file_path = str(tmp_path / "waygate.json")
    b1 = FileBackend(file_path)
    await b1.set_state("/api/test", _make_state())

    b2 = FileBackend(file_path)
    result = await b2.get_state("/api/test")
    assert result.path == "/api/test"


# ---------------------------------------------------------------------------
# RedisBackend-specific: subscribe() via pub/sub
# ---------------------------------------------------------------------------


async def test_redis_subscribe(redis_backend):
    received: list[RouteState] = []

    async def _collect():
        async for state in redis_backend.subscribe():
            received.append(state)
            break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.1)  # give subscribe time to register
    state = _make_state(status=RouteStatus.DISABLED)
    await redis_backend.set_state("/api/test", state)
    await asyncio.wait_for(task, timeout=3)

    assert len(received) == 1
    assert received[0].status == RouteStatus.DISABLED
