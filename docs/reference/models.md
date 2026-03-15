# Models

All models are Pydantic v2 models defined in `shield.core.models`. They are used throughout the engine, backends, and API.

---

## RouteStatus

```python
from shield.core.models import RouteStatus
```

A `StrEnum` representing the lifecycle state of a route:

| Value | Meaning |
|---|---|
| `"active"` | Route is responding normally |
| `"maintenance"` | Route is temporarily unavailable (503) |
| `"disabled"` | Route is permanently off (503) |
| `"env_gated"` | Route is restricted to specific environments (404 elsewhere) |
| `"deprecated"` | Route still works but injects deprecation headers |

---

## MaintenanceWindow

```python
from shield.core.models import MaintenanceWindow
```

Defines a scheduled maintenance period:

| Field | Type | Description |
|---|---|---|
| `start` | `datetime` | When maintenance should activate (UTC) |
| `end` | `datetime` | When maintenance should deactivate (UTC) |
| `reason` | `str` | Optional reason shown in error responses |

```python
from datetime import datetime, UTC
from shield.core.models import MaintenanceWindow

window = MaintenanceWindow(
    start=datetime(2025, 6, 1, 2, 0, tzinfo=UTC),
    end=datetime(2025, 6, 1, 4, 0, tzinfo=UTC),
    reason="Planned migration",
)
```

---

## RouteState

```python
from shield.core.models import RouteState
```

The complete state of a registered route:

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Route key, e.g. `"GET:/payments"` |
| `status` | `RouteStatus` | `ACTIVE` | Current lifecycle state |
| `reason` | `str` | `""` | Human-readable reason for the current status |
| `allowed_envs` | `list[str]` | `[]` | Environments where the route is accessible (for `ENV_GATED`) |
| `allowed_roles` | `list[str]` | `[]` | Roles that bypass maintenance (v0.4+) |
| `allowed_ips` | `list[str]` | `[]` | IPs/CIDRs that bypass maintenance (v0.4+) |
| `window` | `MaintenanceWindow \| None` | `None` | Scheduled maintenance window |
| `sunset_date` | `datetime \| None` | `None` | Sunset date for deprecated routes |
| `successor_path` | `str \| None` | `None` | Successor path for deprecated routes |
| `rollout_percentage` | `int` | `100` | Canary percentage (v0.4+) |

```python
state = RouteState(
    path="GET:/payments",
    status=RouteStatus.MAINTENANCE,
    reason="DB migration",
)

# Serialise
json_str = state.model_dump_json()

# Deserialise
state = RouteState.model_validate_json(json_str)
```

---

## AuditEntry

```python
from shield.core.models import AuditEntry
```

An immutable record of a state change:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID4 identifier |
| `timestamp` | `datetime` | When the change occurred (UTC) |
| `path` | `str` | Route key |
| `action` | `str` | What happened: `"enable"`, `"disable"`, `"maintenance"`, etc. |
| `actor` | `str` | Who made the change (`"system"`, authenticated username, or `"anonymous"`) |
| `platform` | `str` | Source platform: `"cli"`, `"dashboard"`, or `"system"` |
| `reason` | `str` | Optional reason |
| `previous_status` | `RouteStatus` | Status before the change |
| `new_status` | `RouteStatus` | Status after the change |

```python
entries = await engine.get_audit_log(limit=50)

for e in entries:
    print(e.timestamp, e.actor, e.platform, e.action,
          e.path, e.previous_status, "→", e.new_status)
```

---

## GlobalMaintenanceConfig

```python
from shield.core.models import GlobalMaintenanceConfig
```

Configuration for the global maintenance mode:

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Whether global maintenance is active |
| `reason` | `str` | `""` | Shown in all 503 responses |
| `exempt_paths` | `list[str]` | `[]` | Paths that bypass global maintenance |
| `include_force_active` | `bool` | `False` | Whether `@force_active` routes are also blocked |

```python
cfg = await engine.get_global_maintenance()
print(cfg.enabled, cfg.reason, cfg.exempt_paths)
```
