# Exceptions

All shield exceptions are defined in `shield.core.exceptions`. They are raised by `engine.check()` and caught by `ShieldMiddleware` to produce structured error responses.

You generally do not need to import these unless you are building a custom middleware or adapter.

---

## ShieldException

Base class for all shield exceptions.

```python
from shield.core.exceptions import ShieldException
```

---

## MaintenanceException

Raised when a route is in maintenance mode.

```python
from shield.core.exceptions import MaintenanceException
```

| Attribute | Type | Description |
|---|---|---|
| `reason` | `str` | Human-readable maintenance reason |
| `retry_after` | `datetime \| None` | End of maintenance window (for `Retry-After` header) |
| `path` | `str` | Route path |

---

## RouteDisabledException

Raised when a route is permanently disabled.

```python
from shield.core.exceptions import RouteDisabledException
```

| Attribute | Type | Description |
|---|---|---|
| `reason` | `str` | Reason for disabling |
| `path` | `str` | Route path |

---

## EnvGatedException

Raised when a route is restricted to specific environments and the current environment is not in the allowed list.

```python
from shield.core.exceptions import EnvGatedException
```

| Attribute | Type | Description |
|---|---|---|
| `path` | `str` | Route path |
| `current_env` | `str` | The active environment |
| `allowed_envs` | `list[str]` | Environments where the route is accessible |

!!! note
    `ShieldMiddleware` returns a **404 with no body** for `EnvGatedException` — intentionally silent to avoid revealing that the path exists.

---

## Using exceptions in custom adapters

If you are building an adapter for a framework other than FastAPI, catch these exceptions in your middleware/handler:

```python
from shield.core.exceptions import (
    MaintenanceException,
    RouteDisabledException,
    EnvGatedException,
)

try:
    await engine.check(path, method)
except MaintenanceException as exc:
    # Return 503 with Retry-After header
    ...
except RouteDisabledException as exc:
    # Return 503
    ...
except EnvGatedException:
    # Return 404 with no body
    ...
```
