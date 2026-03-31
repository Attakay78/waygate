# Exceptions

All waygate exceptions are defined in `waygate.core.exceptions`. The engine raises them from `engine.check()`, and `WaygateMiddleware` catches them to produce the appropriate HTTP response.

```python
from waygate import (
    WaygateException,
    MaintenanceException,
    RouteDisabledException,
    EnvGatedException,
)
```

!!! note "You usually don't import these directly"
    In a standard FastAPI setup, `WaygateMiddleware` handles all exceptions automatically. You only need to import them if you are building a custom adapter, custom response factory, or writing tests that inspect the raised exception.

---

## Exception hierarchy

```
WaygateException
├── MaintenanceException
├── RouteDisabledException
└── EnvGatedException
```

---

## WaygateException

Base class for all waygate exceptions. Catch this if you want a single handler for any waygate-raised error.

```python
from waygate import WaygateException
```

---

## MaintenanceException

Raised by `engine.check()` when a route (or the entire system via global maintenance) is in maintenance mode.

```python
from waygate import MaintenanceException
```

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `reason` | `str` | Human-readable maintenance reason, passed from the decorator or engine call |
| `retry_after` | `datetime \| None` | End of the maintenance window. The middleware writes this to the `Retry-After` response header so clients know when to retry. `None` if no window was scheduled. |
| `path` | `str` | The route key that triggered the exception |

### In a custom response factory

```python title="accessing exception attributes"
from starlette.requests import Request
from starlette.responses import HTMLResponse
from waygate import MaintenanceException

def maintenance_page(request: Request, exc: MaintenanceException) -> HTMLResponse:
    retry_msg = ""
    if exc.retry_after:
        retry_msg = f"<p>Back at {exc.retry_after.strftime('%H:%M UTC')}</p>"

    return HTMLResponse(
        f"<h1>Under Maintenance</h1><p>{exc.reason}</p>{retry_msg}",
        status_code=503,
    )
```

---

## RouteDisabledException

Raised by `engine.check()` when a route has been permanently disabled.

```python
from waygate import RouteDisabledException
```

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `reason` | `str` | The reason the route was disabled |
| `path` | `str` | The route key |

---

## EnvGatedException

Raised by `engine.check()` when a route is restricted to specific environments and the current environment is not in the allowed list.

```python
from waygate import EnvGatedException
```

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `path` | `str` | The route key |
| `current_env` | `str` | The active environment name (the value `WaygateEngine` was constructed with) |
| `allowed_envs` | `list[str]` | The environments where the route is accessible |

!!! note "WaygateMiddleware returns 403 with JSON"
    When `WaygateMiddleware` catches an `EnvGatedException`, it returns a 403 with a structured JSON body containing `code: "ENV_GATED"`, `current_env`, `allowed_envs`, and `path`.

---

## Using exceptions in custom adapters

If you are building an adapter for a framework other than FastAPI, catch these exceptions in your middleware or request handler and map them to the appropriate HTTP responses.

??? example "Custom adapter middleware pattern"

    ```python
    from waygate import (
        MaintenanceException,
        RouteDisabledException,
        EnvGatedException,
    )

    try:
        await engine.check(path, method)

    except MaintenanceException as exc:
        # 503 with Retry-After header
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = exc.retry_after.isoformat()
        return Response(503, body={"error": exc.reason}, headers=headers)

    except RouteDisabledException as exc:
        # 503, no Retry-After
        return Response(503, body={"error": exc.reason})

    except EnvGatedException as exc:
        # 403 with JSON body
        return Response(403, body={
            "error": {
                "code": "ENV_GATED",
                "message": "This endpoint is not available in the current environment",
                "current_env": exc.current_env,
                "allowed_envs": exc.allowed_envs,
                "path": exc.path,
            }
        })
    ```

See [Building your own adapter](../adapters/custom.md) for a complete working example.
