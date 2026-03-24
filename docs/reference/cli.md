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

## Multi-service commands

### `shield services`

List all distinct service names registered with the Shield Server. Use this to discover which services are currently connected before switching context with `SHIELD_SERVICE`.

```bash
shield services
```

---

### `shield current-service`

Show the active service context (the value of the `SHIELD_SERVICE` environment variable). Useful for confirming which service subsequent commands will target.

```bash
shield current-service
```

**When `SHIELD_SERVICE` is set:**

```
Active service: payments-service  (from SHIELD_SERVICE)
```

**When `SHIELD_SERVICE` is not set:**

```
No active service set.
Set one with: export SHIELD_SERVICE=<service-name>
```

---

## Route commands

Route commands accept an optional `--service` flag to scope to a specific service. All five commands also read the `SHIELD_SERVICE` environment variable as a fallback — an explicit `--service` flag always wins.

```bash
export SHIELD_SERVICE=payments-service   # set once
shield status                            # scoped to payments-service
shield enable GET:/payments              # scoped to payments-service
unset SHIELD_SERVICE
shield status --service orders-service   # explicit flag, no env var needed
```

### `shield status`

Show all registered routes and their current state, or inspect a single route in detail.

```bash
shield status                          # all routes, page 1
shield status GET:/payments            # one route
shield status --page 2                 # next page
shield status --per-page 50           # 50 rows per page
shield status --service payments-service  # scope to one service
```

| Option | Description |
|---|---|
| `--page INT` | Page number to display when listing all routes (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |
| `--service TEXT` | Filter to a specific service. Falls back to `SHIELD_SERVICE` env var. |

**Example output:**

```
┌─────────────────────┬─────────────┬──────────────────────┬──────────────┐
│ Route               │ Status      │ Reason               │ Since        │
├─────────────────────┼─────────────┼──────────────────────┼──────────────┤
│ GET /payments       │ MAINTENANCE │ DB migration         │ 2 hours ago  │
│ GET /debug          │ ENV_GATED   │ dev, staging only    │ startup      │
│ GET /health         │ ACTIVE      │                      │              │
└─────────────────────┴─────────────┴──────────────────────┴──────────────┘
  Showing 1-3  (last page)
```

---

### `shield enable`

Restore a route to `ACTIVE`. Works regardless of the current status.

```bash
shield enable GET:/payments
shield enable GET:/payments --service payments-service
```

| Option | Description |
|---|---|
| `--service TEXT` | Target service. Falls back to `SHIELD_SERVICE` env var. |

---

### `shield disable`

Permanently disable a route. Returns 503 to all callers.

```bash
shield disable GET:/payments
shield disable GET:/payments --reason "Use /v2/payments instead"
shield disable GET:/payments --reason "hotfix" --until 2h
shield disable GET:/payments --service payments-service --reason "hotfix"
```

| Option | Description |
|---|---|
| `--reason TEXT` | Reason shown in error responses and recorded in the audit log |
| `--until DURATION` | Automatically re-enable after this duration. Accepts `2h`, `30m`, `1d`, or an ISO 8601 datetime. |
| `--service TEXT` | Target service. Falls back to `SHIELD_SERVICE` env var. |

---

### `shield maintenance`

Put a route in maintenance mode. Optionally schedule automatic activation and deactivation.

```bash
shield maintenance GET:/payments --reason "DB swap"
shield maintenance GET:/payments --service payments-service --reason "DB swap"
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
| `--service TEXT` | Target service. Falls back to `SHIELD_SERVICE` env var. |

---

### `shield schedule`

Schedule a future maintenance window without activating maintenance now. The route stays `ACTIVE` until `--start` is reached.

```bash
shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"
shield schedule GET:/payments --service payments-service \
  --start 2025-06-01T02:00Z --end 2025-06-01T04:00Z
```

| Option | Description |
|---|---|
| `--start DATETIME` | When to activate maintenance (ISO 8601, required) |
| `--end DATETIME` | When to restore the route to `ACTIVE` (ISO 8601, required) |
| `--reason TEXT` | Reason shown in the 503 response during the window |
| `--service TEXT` | Target service. Falls back to `SHIELD_SERVICE` env var. |

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

## `shield sm` / `shield service-maintenance`

`shield sm` and `shield service-maintenance` are aliases for the same command group. Puts all routes of one service into maintenance mode without affecting other services. The affected SDK client's `app_id` must match the service name.

```bash
shield sm enable payments-service --reason "DB migration"
shield service-maintenance enable payments-service   # identical
```

### `shield sm status`

Show the current maintenance configuration for a service.

```bash
shield sm status <service>
```

```bash
shield sm status payments-service
```

**Example output:**

```
  Service maintenance (payments-service): ON
  Reason               : DB migration
  Include @force_active: no
  Exempt paths         :
    • /health
```

---

### `shield sm enable`

Block all routes of a service immediately. Routes return `503` until `shield sm disable` is called.

```bash
shield sm enable <service>
```

```bash
shield sm enable payments-service --reason "DB migration"
shield sm enable payments-service --reason "Upgrade" --exempt /health --exempt GET:/ready
shield sm enable orders-service --include-force-active
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in every 503 response while maintenance is active |
| `--exempt PATH` | Exempt a path from the block (repeatable). Use bare `/health` or `GET:/health`. |
| `--include-force-active` | Also block `@force_active` routes. Use with care. |

---

### `shield sm disable`

Restore all routes of a service to their individual states.

```bash
shield sm disable <service>
```

```bash
shield sm disable payments-service
```

---

## Rate limit commands

`shield rl` and `shield rate-limits` are aliases for the same command group — use whichever you prefer. Requires `api-shield[rate-limit]` on the server.

```bash
shield rl list          # short form
shield rate-limits list # identical
```

### `shield rl list`

Show all registered rate limit policies.

```bash
shield rl list
shield rl list --page 2
shield rl list --per-page 50
```

| Option | Description |
|---|---|
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

---

### `shield rl set`

Register or update a rate limit policy at runtime. Changes take effect on the next request.

```bash
shield rl set <route> <limit>
```

```bash
shield rl set GET:/public/posts 20/minute
shield rl set GET:/public/posts 5/second --algorithm fixed_window
shield rl set GET:/search 10/minute --key global
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |

---

### `shield rl reset`

Clear all counters for a route immediately. Clients get their full quota back on the next request.

```bash
shield rl reset GET:/public/posts
```

---

### `shield rl delete`

Remove a persisted policy override from the backend.

```bash
shield rl delete GET:/public/posts
```

---

### `shield rl hits`

Show the blocked requests log, newest first. The `Path` column combines the HTTP method and route path.

```bash
shield rl hits                    # page 1, 20 rows
shield rl hits --page 2           # next page
shield rl hits --per-page 50     # 50 rows per page
shield rl hits --route /api/pay   # filter to one route
```

| Option | Description |
|---|---|
| `--route TEXT` | Filter entries to a single route path |
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

---

## Global rate limit commands

`shield grl` and `shield global-rate-limit` are aliases for the same command group. Requires `api-shield[rate-limit]` on the server.

```bash
shield grl get
shield global-rate-limit get   # identical
```

### `shield grl get`

Show the current global rate limit policy, including limit, algorithm, key strategy, burst, exempt routes, and enabled state.

```bash
shield grl get
```

---

### `shield grl set`

Configure the global rate limit. Creates a new policy or replaces the existing one.

```bash
shield grl set <limit>
```

```bash
shield grl set 1000/minute
shield grl set 500/minute --algorithm sliding_window --key ip
shield grl set 2000/hour --burst 50 --exempt /health --exempt GET:/metrics
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `--burst INT` | Extra requests above the base limit |
| `--exempt TEXT` | Exempt route (repeatable). Bare path (`/health`) or method-prefixed (`GET:/metrics`) |

---

### `shield grl delete`

Remove the global rate limit policy entirely.

```bash
shield grl delete
```

---

### `shield grl reset`

Clear all global rate limit counters. The policy is kept; clients get their full quota back on the next request.

```bash
shield grl reset
```

---

### `shield grl enable`

Resume a paused global rate limit policy.

```bash
shield grl enable
```

---

### `shield grl disable`

Pause the global rate limit without removing it. Per-route policies continue to enforce normally.

```bash
shield grl disable
```

---

## `shield srl` / `shield service-rate-limit`

`shield srl` and `shield service-rate-limit` are aliases for the same command group. Manages the rate limit policy for a single service — applies to all routes of that service. Requires `api-shield[rate-limit]` on the server.

```bash
shield srl get payments-service
shield service-rate-limit get payments-service   # identical
```

### `shield srl get`

Show the current rate limit policy for a service, including limit, algorithm, key strategy, burst, exempt routes, and enabled state.

```bash
shield srl get <service>
```

```bash
shield srl get payments-service
```

---

### `shield srl set`

Configure the rate limit for a service. Creates a new policy or replaces the existing one.

```bash
shield srl set <service> <limit>
```

```bash
shield srl set payments-service 1000/minute
shield srl set payments-service 500/minute --algorithm sliding_window --key ip
shield srl set payments-service 2000/hour --burst 50 --exempt /health --exempt GET:/metrics
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `--burst INT` | Extra requests above the base limit |
| `--exempt TEXT` | Exempt route (repeatable). Bare path (`/health`) or method-prefixed (`GET:/metrics`) |

---

### `shield srl delete`

Remove the service rate limit policy entirely.

```bash
shield srl delete <service>
```

```bash
shield srl delete payments-service
```

---

### `shield srl reset`

Clear all counters for the service. The policy is kept; clients get their full quota back on the next request.

```bash
shield srl reset <service>
```

```bash
shield srl reset payments-service
```

---

### `shield srl enable`

Resume a paused service rate limit policy.

```bash
shield srl enable <service>
```

---

### `shield srl disable`

Pause the service rate limit without removing it. Per-route policies continue to enforce normally.

```bash
shield srl disable <service>
```

---

## Audit log

### `shield log`

Display the audit log, newest entries first. The `Status` column shows `old > new` for route state changes and a coloured action label for rate limit policy changes (including global RL actions such as `global set`, `global reset`, `global enabled`, `global disabled`, and service RL actions such as `svc set`, `svc reset`, `svc enabled`, `svc disabled`). The `Path` column shows human-readable labels for sentinel-keyed entries: `[Global Maintenance]`, `[Global Rate Limit]`, `[{service} Maintenance]`, and `[{service} Rate Limit]`.

```bash
shield log                          # page 1, 20 rows
shield log --route GET:/payments    # filter by route
shield log --page 2                 # next page
shield log --per-page 50           # 50 rows per page
```

| Option | Description |
|---|---|
| `--route ROUTE` | Filter entries to a single route key |
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

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
