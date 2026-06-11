# Testing

Routes decorated with `@maintenance`, `@disabled`, `@env_only`, or `@rate_limit` interfere with tests by design: they block or throttle requests the same way they do in production. This page explains how to disable those checks selectively so your tests can focus on business logic.

---

## The two bypass flags

`WaygateEngine` exposes two independent flags:

| Flag | What it disables |
|---|---|
| `bypass_lifecycle` | Maintenance, disabled, and env-gated checks. Every route is treated as active. |
| `bypass_rate_limits` | All rate limit checks. Requests pass without consuming quota. |

Both default to `False`. You can set them independently, so you can disable rate limiting while keeping lifecycle checks (or vice versa).

---

## Option 1: constructor flag

Pass the flags when creating the engine. Use this when you have a dedicated test engine instance.

```python title="tests/conftest.py"
import pytest
from fastapi.testclient import TestClient
from waygate import WaygateEngine, MemoryBackend
from waygate.fastapi import WaygateMiddleware, WaygateRouter

from myapp import create_app

@pytest.fixture
def client():
    engine = WaygateEngine(
        backend=MemoryBackend(),
        bypass_rate_limits=True,
        bypass_lifecycle=True,
    )
    app = create_app(engine=engine)
    return TestClient(app)
```

With `WaygateSDK`, pass the same flags to the SDK constructor:

```python title="tests/conftest.py"
from waygate.sdk import WaygateSDK

sdk = WaygateSDK(
    server_url="http://waygate-server:9000",
    app_id="payments-service",
    bypass_rate_limits=True,
    bypass_lifecycle=True,
)
```

---

## Option 2: environment variable

Set `WAYGATE_BYPASS_RATE_LIMITS` or `WAYGATE_BYPASS_LIFECYCLE` before running tests. This works without changing any application code.

```bash
WAYGATE_BYPASS_RATE_LIMITS=1 pytest
WAYGATE_BYPASS_LIFECYCLE=1 pytest

# both at once
WAYGATE_BYPASS_RATE_LIMITS=1 WAYGATE_BYPASS_LIFECYCLE=1 pytest
```

Or set them in `pytest.ini`:

```ini title="pytest.ini"
[pytest]
env =
    WAYGATE_BYPASS_RATE_LIMITS=1
    WAYGATE_BYPASS_LIFECYCLE=1
```

!!! note
    `pytest-env` is required for the `pytest.ini` approach. Install it with `uv add pytest-env --dev`.

---

## Option 3: `bypass()` context manager

`waygate.testing.bypass` lets you disable checks for a specific block of code. The original flags are restored when the block exits, even if an exception is raised.

```python title="tests/test_payments.py"
from waygate.testing import bypass

def test_payment_over_limit(client, engine):
    # exhaust the rate limit
    for _ in range(10):
        client.get("/api/payments")

    # now test the 429 response
    response = client.get("/api/payments")
    assert response.status_code == 429

    # bypass rate limits for the next assertion
    with bypass(engine, rate_limits=True, lifecycle=False):
        response = client.get("/api/payments")
        assert response.status_code == 200
```

`bypass()` parameters:

| Parameter | Default | Description |
|---|---|---|
| `engine` | required | The `WaygateEngine` instance to modify. |
| `rate_limits` | `True` | Disable rate limit checks inside the block. |
| `lifecycle` | `True` | Disable maintenance, disabled, and env-gated checks inside the block. |

Both parameters default to `True`, so `bypass(engine)` disables everything.

---

## Choosing the right approach

| Approach | Best for |
|---|---|
| Constructor flag | Dedicated test engine instance in `conftest.py` |
| Env var | Bypassing checks for the entire test session without code changes |
| Context manager | Targeting specific test blocks while keeping checks active elsewhere |

---

## What bypass does NOT affect

- The `@force_active` decorator always passes, regardless of bypass settings.
- Backend reads and writes still happen normally. `engine.disable()` and other mutating calls still persist state.
- Webhook notifications still fire.
- The audit log still records state changes.

The bypass flags only affect the enforcement step inside `engine.check()`.
