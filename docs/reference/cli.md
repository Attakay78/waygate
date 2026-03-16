# CLI Reference

The `shield` CLI lets you manage routes, view the audit log, and control global maintenance from the terminal. It communicates with a running `ShieldAdmin` instance over HTTP — it does not access the backend directly.

```bash
uv add "api-shield[cli]"
```

!!! tip "Set the server URL first"
    Before using any commands, point the CLI at your running server:

    ```bash
    shield config set-url http://localhost:8000/shield
    shield login admin
    ```

    See [Server URL discovery](#server-url-discovery) for other ways to configure the URL.

---

## Auth commands

### `shield login`

Authenticate with a `ShieldAdmin` server and store the token locally. The CLI will prompt for a password if `--password` is omitted.

```bash
shield login <username>
```

```bash
shield login admin                     # prompts for password interactively
shield login admin --password secret   # inline, useful in CI pipelines
```

| Option | Description |
|---|---|
| `--password TEXT` | Password for the given username. Omit to be prompted securely. |

Tokens are saved to `~/.shield/config.json` with an expiry timestamp. The CLI automatically uses the stored token for all subsequent commands until it expires or you log out.

---

### `shield logout`

Revoke the server-side token and clear local credentials.

```bash
shield logout
```

---

## Route commands

### `shield status`

Show all registered routes and their current state, or inspect a single route in detail.

```bash
shield status                  # all routes
shield status GET:/payments    # one route
```

**Example output:**

```
┌─────────────────────┬─────────────┬──────────────────────┬──────────────┐
│ Route               │ Status      │ Reason               │ Since        │
├─────────────────────┼─────────────┼──────────────────────┼──────────────┤
│ GET /payments       │ MAINTENANCE │ DB migration         │ 2 hours ago  │
│ GET /debug          │ ENV_GATED   │ dev, staging only    │ startup      │
│ GET /health         │ ACTIVE      │                      │              │
└─────────────────────┴─────────────┴──────────────────────┴──────────────┘
```

---

### `shield enable`

Restore a route to `ACTIVE`. Works regardless of the current status.

```bash
shield enable GET:/payments
```

---

### `shield disable`

Permanently disable a route. Returns 503 to all callers.

```bash
shield disable GET:/payments
shield disable GET:/payments --reason "Use /v2/payments instead"
shield disable GET:/payments --reason "hotfix" --until 2h
```

| Option | Description |
|---|---|
| `--reason TEXT` | Reason shown in error responses and recorded in the audit log |
| `--until DURATION` | Automatically re-enable after this duration. Accepts `2h`, `30m`, `1d`, or an ISO 8601 datetime. |

---

### `shield maintenance`

Put a route in maintenance mode. Optionally schedule automatic activation and deactivation.

```bash
shield maintenance GET:/payments --reason "DB swap"
```

```bash
# Scheduled window
shield maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in the 503 error response |
| `--start DATETIME` | Start of the maintenance window (ISO 8601). Maintenance activates automatically at this time. |
| `--end DATETIME` | End of the maintenance window. Sets the `Retry-After` header and restores `ACTIVE` automatically. |

---

### `shield schedule`

Schedule a future maintenance window without activating maintenance now. The route stays `ACTIVE` until `--start` is reached.

```bash
shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"
```

| Option | Description |
|---|---|
| `--start DATETIME` | When to activate maintenance (ISO 8601, required) |
| `--end DATETIME` | When to restore the route to `ACTIVE` (ISO 8601, required) |
| `--reason TEXT` | Reason shown in the 503 response during the window |

---

## Global maintenance commands

Global maintenance blocks every non-exempt route at once, without requiring individual route changes.

### `shield global status`

Show the current global maintenance state, including whether it is active, the reason, and any exempt paths.

```bash
shield global status
```

---

### `shield global enable`

Block all non-exempt routes immediately.

```bash
shield global enable --reason "Deploying v2"
shield global enable --reason "Deploying v2" --exempt /health --exempt GET:/status
shield global enable --reason "Hard lockdown" --include-force-active
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in every 503 response while global maintenance is active |
| `--exempt PATH` | Exempt a path from the global block (repeatable). Use bare `/health` for any method, or `GET:/health` for a specific method. |
| `--include-force-active` | Block `@force_active` routes too. Use with care — this will block health checks and readiness probes. |

!!! warning "Exempting health checks"
    Always exempt your health and readiness probe endpoints before enabling global maintenance, unless you intend to take the instance out of rotation:

    ```bash
    shield global enable --reason "Deploying v2" --exempt /health --exempt /ready
    ```

---

### `shield global disable`

Restore all routes to their individual states. Each route resumes the status it had before global maintenance was enabled.

```bash
shield global disable
```

---

### `shield global exempt-add`

Add a path to the exemption list while global maintenance is already active, without toggling the mode.

```bash
shield global exempt-add /monitoring/ping
```

---

### `shield global exempt-remove`

Remove a path from the exemption list.

```bash
shield global exempt-remove /monitoring/ping
```

---

## Audit log

### `shield log`

Display the audit log, newest entries first.

```bash
shield log                          # last 20 entries
shield log --route GET:/payments    # filter by route
shield log --limit 100              # show more entries
```

| Option | Description |
|---|---|
| `--route ROUTE` | Filter entries to a single route key |
| `--limit INT` | Maximum number of entries to display (default: 20) |

---

## Config commands

### `shield config set-url`

Override the server URL and save it to `~/.shield/config.json`. All subsequent commands will use this URL.

```bash
shield config set-url http://prod.example.com/shield
```

---

### `shield config show`

Display the resolved server URL, its source (env var, `.shield` file, or config file), and the current auth session status.

```bash
shield config show
```

---

## Server URL discovery

The CLI resolves the server URL using the following priority order — highest wins:

| Priority | Source | Example |
|---|---|---|
| 1 (highest) | `SHIELD_SERVER_URL` environment variable | `export SHIELD_SERVER_URL=http://...` |
| 2 | `SHIELD_SERVER_URL` in a `.shield` file (walked up from the current directory) | `.shield` file in project root |
| 3 | `server_url` in `~/.shield/config.json` | Set via `shield config set-url` |
| 4 (default) | Hard-coded default | `http://localhost:8000/shield` |

!!! tip "Commit a `.shield` file"
    Add a `.shield` file to your project root so the whole team automatically uses the correct server URL without manual configuration:

    ```ini title=".shield"
    SHIELD_SERVER_URL=http://localhost:8000/shield
    ```

---

## Route key format

Routes are identified by a method-prefixed key. Use the same format in all CLI commands.

| Decorator | Route key |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |
| `@router.get("/api/v1/users")` | `GET:/api/v1/users` |

```bash
shield disable "GET:/payments"    # specific method
shield enable "/payments"         # applies to all methods registered under /payments
```

---

## Token storage

Auth tokens are stored in a JSON file at a platform-specific location:

| Platform | Location |
|---|---|
| macOS / Linux | `~/.shield/config.json` |
| Windows | `%USERPROFILE%\AppData\Local\shield\config.json` |

The config file stores the server URL, the current token, the username, and the token expiry timestamp. Delete this file to clear all credentials.
