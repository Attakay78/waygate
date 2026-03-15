# Admin Dashboard

`ShieldAdmin` is the unified admin interface — it mounts the HTMX dashboard UI and the REST API (used by the CLI) under a single path.

---

## Mounting ShieldAdmin

```python
from fastapi import FastAPI
from shield.core.engine import ShieldEngine
from shield.fastapi.middleware import ShieldMiddleware
from shield.admin import ShieldAdmin

engine = ShieldEngine()

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

# Mount at /shield — exposes dashboard UI + REST API
app.mount(
    "/shield",
    ShieldAdmin(
        engine=engine,
        auth=("admin", "secret"),
        prefix="/shield",
    ),
)
```

After starting the server:

- **Dashboard UI**: `http://localhost:8000/shield/`
- **Audit log**: `http://localhost:8000/shield/audit`
- **REST API**: `http://localhost:8000/shield/api/`

---

## Authentication

`auth=` accepts three forms:

=== "Single user"

    ```python
    ShieldAdmin(engine=engine, auth=("admin", "secret"))
    ```

=== "Multiple users"

    ```python
    ShieldAdmin(engine=engine, auth=[("alice", "pass1"), ("bob", "pass2")])
    ```

=== "Custom auth backend"

    ```python
    from shield.admin.auth import ShieldAuthBackend

    class MyDBAuth(ShieldAuthBackend):
        def authenticate_user(self, username: str, password: str) -> bool:
            return db.check(username, password)

    ShieldAdmin(engine=engine, auth=MyDBAuth())
    ```

=== "No auth (open access)"

    ```python
    ShieldAdmin(engine=engine)
    ```

!!! tip "Token invalidation"
    When you change `auth=` (new user, changed password), all previously issued tokens are automatically invalidated on restart — even if `secret_key` is stable. This is handled by mixing an auth fingerprint into the HMAC signing key.

---

## Dashboard UI

<figure class="screenshot" markdown>
  ![Shield Admin Dashboard](../assets/dashboard.png)
  <figcaption>The admin dashboard showing route states, status badges, and per-route actions.</figcaption>
</figure>

The dashboard renders all registered routes with live status badges:

| Status | Colour | Description |
|---|---|---|
| `ACTIVE` | Green | Route responding normally |
| `MAINTENANCE` | Yellow | Route returning 503 |
| `DISABLED` | Red | Route permanently off |
| `ENV_GATED` | Blue | Route restricted to specific environments |
| `DEPRECATED` | Grey | Route still works but headers warn clients |

### Actions per route

- **Enable** — restore route to `ACTIVE`
- **Maintenance** — put in maintenance with optional reason + window
- **Disable** — permanently disable with reason

### Live updates (SSE)

The dashboard connects to the `/shield/events` SSE endpoint. When state changes (from another browser tab, CLI command, or API call), the affected row updates in real time without a page reload.

### Audit log

`http://localhost:8000/shield/audit` shows a paginated table of all state changes:

- Timestamp
- Route
- Action (enable / disable / maintenance / etc.)
- Actor (authenticated username or "anonymous")
- Platform (`cli` or `dashboard`)
- Old status → New status
- Reason

---

## REST API

The same mount exposes a JSON API used by the `shield` CLI:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/login` | Exchange credentials for a bearer token |
| `POST` | `/api/auth/logout` | Revoke the current token |
| `GET` | `/api/auth/me` | Current actor info |
| `GET` | `/api/routes` | List all route states |
| `GET` | `/api/routes/{key}` | Get one route |
| `POST` | `/api/routes/{key}/enable` | Enable a route |
| `POST` | `/api/routes/{key}/disable` | Disable a route |
| `POST` | `/api/routes/{key}/maintenance` | Put route in maintenance |
| `POST` | `/api/routes/{key}/schedule` | Schedule a maintenance window |
| `DELETE` | `/api/routes/{key}/schedule` | Cancel a scheduled window |
| `GET` | `/api/audit` | Audit log (`?route=` and `?limit=` params) |
| `GET` | `/api/global` | Global maintenance config |
| `POST` | `/api/global/enable` | Enable global maintenance |
| `POST` | `/api/global/disable` | Disable global maintenance |

---

## Advanced options

```python
ShieldAdmin(
    engine=engine,
    auth=("admin", "secret"),
    prefix="/shield",             # must match the mount path
    secret_key="stable-key",      # omit in dev; set for production to survive restarts
    token_expiry=86400,           # token lifetime in seconds (default: 24 h)
)
```

| Option | Default | Description |
|---|---|---|
| `engine` | required | The `ShieldEngine` instance |
| `auth` | `None` (open) | Credentials — see forms above |
| `prefix` | `"/shield"` | Mount path prefix (must match `app.mount()`) |
| `secret_key` | random | HMAC signing key — set a stable value in production |
| `token_expiry` | `86400` | Token lifetime in seconds |

---

## Next step

[**Tutorial: CLI →**](cli.md)
