# Models

All models are Pydantic v2 models defined in `shield.core.models`. They are the shared data structures used throughout the engine, backends, API, and CLI.

```python
from shield.core.models import RouteStatus, RouteState, MaintenanceWindow, AuditEntry, GlobalMaintenanceConfig
```

---

## RouteStatus

A `StrEnum` representing the lifecycle state of a route. Because it extends `str`, you can compare values against plain strings and store them in JSON without conversion.

```python
from shield.core.models import RouteStatus
```

| Value | String | Meaning |
|---|---|---|
| `RouteStatus.ACTIVE` | `"active"` | Route is responding normally |
| `RouteStatus.MAINTENANCE` | `"maintenance"` | Route is temporarily unavailable; returns 503 |
| `RouteStatus.DISABLED` | `"disabled"` | Route is permanently off; returns 503 |
| `RouteStatus.ENV_GATED` | `"env_gated"` | Route is restricted to specific environments; returns 404 elsewhere |
| `RouteStatus.DEPRECATED` | `"deprecated"` | Route still responds but injects deprecation headers |

```python title="comparing statuses"
state = await engine.get_state("GET:/payments")

if state.status == RouteStatus.MAINTENANCE:
    print("Route is under maintenance")

# StrEnum means string comparison also works
if state.status == "maintenance":
    print("Route is under maintenance")
```

---

## MaintenanceWindow

Defines a scheduled maintenance period with a start and end time. Pass a `MaintenanceWindow` to `@maintenance(start=..., end=...)` or to `engine.set_maintenance()` to schedule automatic activation and deactivation.

```python
from shield.core.models import MaintenanceWindow
```

### Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `start` | `datetime` | Yes | When maintenance should activate. Should be timezone-aware (UTC). |
| `end` | `datetime` | Yes | When maintenance should deactivate. Sets the `Retry-After` response header. |
| `reason` | `str` | No | Optional human-readable reason shown in error responses during the window. Defaults to `""`. |

### Example

```python title="creating a maintenance window"
from datetime import datetime, UTC
from shield.core.models import MaintenanceWindow

window = MaintenanceWindow(
    start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
    end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
    reason="Planned database migration",
)
```

!!! tip "Use UTC datetimes"
    Always pass timezone-aware datetimes to avoid ambiguity. `datetime(..., tzinfo=UTC)` or `datetime.now(UTC)` are reliable choices.

---

## RouteState

The complete, current state of a registered route. This is what backends store and what the engine reads on every `check()` call.

```python
from shield.core.models import RouteState
```

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Route key in `METHOD:/path` format, e.g. `"GET:/payments"`. Read more in [route key format](../reference/cli.md#route-key-format). |
| `status` | `RouteStatus` | `ACTIVE` | Current lifecycle state. Read more in [RouteStatus](#routestatus). |
| `reason` | `str` | `""` | Human-readable reason for the current status. Shown in error responses and the admin dashboard. |
| `allowed_envs` | `list[str]` | `[]` | Environments where the route is accessible. Only relevant when `status` is `ENV_GATED`. |
| `allowed_roles` | `list[str]` | `[]` | Roles that bypass maintenance mode. *(v0.4+)* |
| `allowed_ips` | `list[str]` | `[]` | IPs or CIDR blocks that bypass maintenance mode. *(v0.4+)* |
| `window` | `MaintenanceWindow \| None` | `None` | Scheduled maintenance window. Read more in [MaintenanceWindow](#maintenancewindow). |
| `sunset_date` | `datetime \| None` | `None` | The date the route will be removed. Used by `@deprecated`. |
| `successor_path` | `str \| None` | `None` | Path or URL of the replacement resource. Used by `@deprecated` to populate the `Link` header. |
| `rollout_percentage` | `int` | `100` | Percentage of requests that reach this route during a canary rollout. *(v0.4+)* |

??? example "Creating and serialising a RouteState"

    ```python
    from shield.core.models import RouteState, RouteStatus

    state = RouteState(
        path="GET:/payments",
        status=RouteStatus.MAINTENANCE,
        reason="DB migration in progress",
    )

    # Serialise to a JSON string (Pydantic v2)
    json_str = state.model_dump_json()

    # Deserialise from a JSON string
    restored = RouteState.model_validate_json(json_str)
    ```

!!! note "You rarely construct `RouteState` directly"
    In normal usage, `RouteState` objects are created and managed by the engine. You read them via `engine.get_state()` or `engine.list_states()`, and write them via `engine.set_maintenance()`, `engine.disable()`, etc.

---

## AuditEntry

An immutable record of a single state change. Every call to `engine.enable()`, `engine.disable()`, `engine.set_maintenance()`, or any other state-mutating method writes an `AuditEntry` to the backend.

```python
from shield.core.models import AuditEntry
```

### Fields

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID4 identifier for this entry |
| `timestamp` | `datetime` | When the change occurred (UTC) |
| `path` | `str` | Route key that was changed |
| `action` | `str` | What happened: `"enable"`, `"disable"`, `"maintenance"`, `"env_only"`, `"schedule"`, etc. |
| `actor` | `str` | Who made the change. One of: a username from the admin dashboard or CLI, `"system"` for scheduler-driven changes, or `"anonymous"` for unauthenticated requests. |
| `platform` | `str` | Where the change originated: `"cli"`, `"dashboard"`, or `"system"` |
| `reason` | `str` | Optional reason, passed through from the state-change call |
| `previous_status` | `RouteStatus` | The route's status before the change |
| `new_status` | `RouteStatus` | The route's status after the change |

??? example "Reading the audit log"

    ```python
    entries = await engine.get_audit_log(limit=50)

    for e in entries:
        print(
            e.timestamp.isoformat(),
            e.actor,
            f"[{e.platform}]",
            e.action,
            e.path,
            f"{e.previous_status} → {e.new_status}",
        )
    ```

Read more in [ShieldEngine: get_audit_log](engine.md#get_audit_log).

---

## GlobalMaintenanceConfig

The configuration object for global maintenance mode. Returned by `engine.get_global_maintenance()`.

```python
from shield.core.models import GlobalMaintenanceConfig
```

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Whether global maintenance is currently active |
| `reason` | `str` | `""` | Shown in every 503 response while global maintenance is active |
| `exempt_paths` | `list[str]` | `[]` | Paths that bypass global maintenance and respond normally. Use bare `/health` (any method) or `GET:/health` (specific method). |
| `include_force_active` | `bool` | `False` | When `True`, even `@force_active` routes are blocked |

### Example

```python title="checking global maintenance state"
cfg = await engine.get_global_maintenance()

if cfg.enabled:
    print(f"Global maintenance ON: {cfg.reason}")
    print(f"Exempt paths: {cfg.exempt_paths}")
else:
    print("All systems normal")
```

Read more in [ShieldEngine: global maintenance](engine.md#global-maintenance).
