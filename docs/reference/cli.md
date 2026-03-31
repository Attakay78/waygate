# CLI Reference

The `waygate` CLI lets you manage routes, view the audit log, and control global maintenance from the terminal. It communicates with a running `WaygateAdmin` instance over HTTP — it does not access the backend directly.

```bash
uv add "waygate[cli]"
```

!!! tip "Set the server URL first"
    Before using any commands, point the CLI at your running server:

    ```bash
    waygate config set-url http://localhost:8000/waygate
    waygate login admin
    ```

    See [Server URL discovery](#server-url-discovery) for other ways to configure the URL.

---

## Auth commands

### `waygate login`

Authenticate with a `WaygateAdmin` server and store the token locally. The CLI will prompt for a password if `--password` is omitted.

```bash
waygate login <username>
```

```bash
waygate login admin                     # prompts for password interactively
waygate login admin --password secret   # inline, useful in CI pipelines
```

| Option | Description |
|---|---|
| `--password TEXT` | Password for the given username. Omit to be prompted securely. |

Tokens are saved to `~/.waygate/config.json` with an expiry timestamp. The CLI automatically uses the stored token for all subsequent commands until it expires or you log out.

---

### `waygate logout`

Revoke the server-side token and clear local credentials.

```bash
waygate logout
```

---

## Multi-service commands

### `waygate services`

List all distinct service names registered with the Waygate Server. Use this to discover which services are currently connected before switching context with `WAYGATE_SERVICE`.

```bash
waygate services
```

---

### `waygate current-service`

Show the active service context (the value of the `WAYGATE_SERVICE` environment variable). Useful for confirming which service subsequent commands will target.

```bash
waygate current-service
```

**When `WAYGATE_SERVICE` is set:**

```
Active service: payments-service  (from WAYGATE_SERVICE)
```

**When `WAYGATE_SERVICE` is not set:**

```
No active service set.
Set one with: export WAYGATE_SERVICE=<service-name>
```

---

## Route commands

Route commands accept an optional `--service` flag to scope to a specific service. All five commands also read the `WAYGATE_SERVICE` environment variable as a fallback — an explicit `--service` flag always wins.

```bash
export WAYGATE_SERVICE=payments-service   # set once
waygate status                            # scoped to payments-service
waygate enable GET:/payments              # scoped to payments-service
unset WAYGATE_SERVICE
waygate status --service orders-service   # explicit flag, no env var needed
```

### `waygate status`

Show all registered routes and their current state, or inspect a single route in detail.

```bash
waygate status                          # all routes, page 1
waygate status GET:/payments            # one route
waygate status --page 2                 # next page
waygate status --per-page 50           # 50 rows per page
waygate status --service payments-service  # scope to one service
```

| Option | Description |
|---|---|
| `--page INT` | Page number to display when listing all routes (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |
| `--service TEXT` | Filter to a specific service. Falls back to `WAYGATE_SERVICE` env var. |

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

### `waygate enable`

Restore a route to `ACTIVE`. Works regardless of the current status.

```bash
waygate enable GET:/payments
waygate enable GET:/payments --service payments-service
```

| Option | Description |
|---|---|
| `--service TEXT` | Target service. Falls back to `WAYGATE_SERVICE` env var. |

---

### `waygate disable`

Permanently disable a route. Returns 503 to all callers.

```bash
waygate disable GET:/payments
waygate disable GET:/payments --reason "Use /v2/payments instead"
waygate disable GET:/payments --reason "hotfix" --until 2h
waygate disable GET:/payments --service payments-service --reason "hotfix"
```

| Option | Description |
|---|---|
| `--reason TEXT` | Reason shown in error responses and recorded in the audit log |
| `--until DURATION` | Automatically re-enable after this duration. Accepts `2h`, `30m`, `1d`, or an ISO 8601 datetime. |
| `--service TEXT` | Target service. Falls back to `WAYGATE_SERVICE` env var. |

---

### `waygate maintenance`

Put a route in maintenance mode. Optionally schedule automatic activation and deactivation.

```bash
waygate maintenance GET:/payments --reason "DB swap"
waygate maintenance GET:/payments --service payments-service --reason "DB swap"
```

```bash
# Scheduled window
waygate maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in the 503 error response |
| `--start DATETIME` | Start of the maintenance window (ISO 8601). Maintenance activates automatically at this time. |
| `--end DATETIME` | End of the maintenance window. Sets the `Retry-After` header and restores `ACTIVE` automatically. |
| `--service TEXT` | Target service. Falls back to `WAYGATE_SERVICE` env var. |

---

### `waygate schedule`

Schedule a future maintenance window without activating maintenance now. The route stays `ACTIVE` until `--start` is reached.

```bash
waygate schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"
waygate schedule GET:/payments --service payments-service \
  --start 2025-06-01T02:00Z --end 2025-06-01T04:00Z
```

| Option | Description |
|---|---|
| `--start DATETIME` | When to activate maintenance (ISO 8601, required) |
| `--end DATETIME` | When to restore the route to `ACTIVE` (ISO 8601, required) |
| `--reason TEXT` | Reason shown in the 503 response during the window |
| `--service TEXT` | Target service. Falls back to `WAYGATE_SERVICE` env var. |

---

## Global maintenance commands

Global maintenance blocks every non-exempt route at once, without requiring individual route changes.

### `waygate global status`

Show the current global maintenance state, including whether it is active, the reason, and any exempt paths.

```bash
waygate global status
```

---

### `waygate global enable`

Block all non-exempt routes immediately.

```bash
waygate global enable --reason "Deploying v2"
waygate global enable --reason "Deploying v2" --exempt /health --exempt GET:/status
waygate global enable --reason "Hard lockdown" --include-force-active
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in every 503 response while global maintenance is active |
| `--exempt PATH` | Exempt a path from the global block (repeatable). Use bare `/health` for any method, or `GET:/health` for a specific method. |
| `--include-force-active` | Block `@force_active` routes too. Use with care — this will block health checks and readiness probes. |

!!! warning "Exempting health checks"
    Always exempt your health and readiness probe endpoints before enabling global maintenance, unless you intend to take the instance out of rotation:

    ```bash
    waygate global enable --reason "Deploying v2" --exempt /health --exempt /ready
    ```

---

### `waygate global disable`

Restore all routes to their individual states. Each route resumes the status it had before global maintenance was enabled.

```bash
waygate global disable
```

---

### `waygate global exempt-add`

Add a path to the exemption list while global maintenance is already active, without toggling the mode.

```bash
waygate global exempt-add /monitoring/ping
```

---

### `waygate global exempt-remove`

Remove a path from the exemption list.

```bash
waygate global exempt-remove /monitoring/ping
```

---

## `waygate sm` / `waygate service-maintenance`

`waygate sm` and `waygate service-maintenance` are aliases for the same command group. Puts all routes of one service into maintenance mode without affecting other services. The affected SDK client's `app_id` must match the service name.

```bash
waygate sm enable payments-service --reason "DB migration"
waygate service-maintenance enable payments-service   # identical
```

### `waygate sm status`

Show the current maintenance configuration for a service.

```bash
waygate sm status <service>
```

```bash
waygate sm status payments-service
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

### `waygate sm enable`

Block all routes of a service immediately. Routes return `503` until `waygate sm disable` is called.

```bash
waygate sm enable <service>
```

```bash
waygate sm enable payments-service --reason "DB migration"
waygate sm enable payments-service --reason "Upgrade" --exempt /health --exempt GET:/ready
waygate sm enable orders-service --include-force-active
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in every 503 response while maintenance is active |
| `--exempt PATH` | Exempt a path from the block (repeatable). Use bare `/health` or `GET:/health`. |
| `--include-force-active` | Also block `@force_active` routes. Use with care. |

---

### `waygate sm disable`

Restore all routes of a service to their individual states.

```bash
waygate sm disable <service>
```

```bash
waygate sm disable payments-service
```

---

## Rate limit commands

`waygate rl` and `waygate rate-limits` are aliases for the same command group — use whichever you prefer. Requires `waygate[rate-limit]` on the server.

```bash
waygate rl list          # short form
waygate rate-limits list # identical
```

### `waygate rl list`

Show all registered rate limit policies.

```bash
waygate rl list
waygate rl list --page 2
waygate rl list --per-page 50
```

| Option | Description |
|---|---|
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

---

### `waygate rl set`

Register or update a rate limit policy at runtime. Changes take effect on the next request.

```bash
waygate rl set <route> <limit>
```

```bash
waygate rl set GET:/public/posts 20/minute
waygate rl set GET:/public/posts 5/second --algorithm fixed_window
waygate rl set GET:/search 10/minute --key global
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |

---

### `waygate rl reset`

Clear all counters for a route immediately. Clients get their full quota back on the next request.

```bash
waygate rl reset GET:/public/posts
```

---

### `waygate rl delete`

Remove a persisted policy override from the backend.

```bash
waygate rl delete GET:/public/posts
```

---

### `waygate rl hits`

Show the blocked requests log, newest first. The `Path` column combines the HTTP method and route path.

```bash
waygate rl hits                    # page 1, 20 rows
waygate rl hits --page 2           # next page
waygate rl hits --per-page 50     # 50 rows per page
waygate rl hits --route /api/pay   # filter to one route
```

| Option | Description |
|---|---|
| `--route TEXT` | Filter entries to a single route path |
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

---

## Global rate limit commands

`waygate grl` and `waygate global-rate-limit` are aliases for the same command group. Requires `waygate[rate-limit]` on the server.

```bash
waygate grl get
waygate global-rate-limit get   # identical
```

### `waygate grl get`

Show the current global rate limit policy, including limit, algorithm, key strategy, burst, exempt routes, and enabled state.

```bash
waygate grl get
```

---

### `waygate grl set`

Configure the global rate limit. Creates a new policy or replaces the existing one.

```bash
waygate grl set <limit>
```

```bash
waygate grl set 1000/minute
waygate grl set 500/minute --algorithm sliding_window --key ip
waygate grl set 2000/hour --burst 50 --exempt /health --exempt GET:/metrics
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `--burst INT` | Extra requests above the base limit |
| `--exempt TEXT` | Exempt route (repeatable). Bare path (`/health`) or method-prefixed (`GET:/metrics`) |

---

### `waygate grl delete`

Remove the global rate limit policy entirely.

```bash
waygate grl delete
```

---

### `waygate grl reset`

Clear all global rate limit counters. The policy is kept; clients get their full quota back on the next request.

```bash
waygate grl reset
```

---

### `waygate grl enable`

Resume a paused global rate limit policy.

```bash
waygate grl enable
```

---

### `waygate grl disable`

Pause the global rate limit without removing it. Per-route policies continue to enforce normally.

```bash
waygate grl disable
```

---

## `waygate srl` / `waygate service-rate-limit`

`waygate srl` and `waygate service-rate-limit` are aliases for the same command group. Manages the rate limit policy for a single service — applies to all routes of that service. Requires `waygate[rate-limit]` on the server.

```bash
waygate srl get payments-service
waygate service-rate-limit get payments-service   # identical
```

### `waygate srl get`

Show the current rate limit policy for a service, including limit, algorithm, key strategy, burst, exempt routes, and enabled state.

```bash
waygate srl get <service>
```

```bash
waygate srl get payments-service
```

---

### `waygate srl set`

Configure the rate limit for a service. Creates a new policy or replaces the existing one.

```bash
waygate srl set <service> <limit>
```

```bash
waygate srl set payments-service 1000/minute
waygate srl set payments-service 500/minute --algorithm sliding_window --key ip
waygate srl set payments-service 2000/hour --burst 50 --exempt /health --exempt GET:/metrics
```

| Option | Description |
|---|---|
| `--algorithm TEXT` | Counting algorithm: `fixed_window`, `sliding_window`, `moving_window`, `token_bucket` |
| `--key TEXT` | Key strategy: `ip`, `user`, `api_key`, `global` |
| `--burst INT` | Extra requests above the base limit |
| `--exempt TEXT` | Exempt route (repeatable). Bare path (`/health`) or method-prefixed (`GET:/metrics`) |

---

### `waygate srl delete`

Remove the service rate limit policy entirely.

```bash
waygate srl delete <service>
```

```bash
waygate srl delete payments-service
```

---

### `waygate srl reset`

Clear all counters for the service. The policy is kept; clients get their full quota back on the next request.

```bash
waygate srl reset <service>
```

```bash
waygate srl reset payments-service
```

---

### `waygate srl enable`

Resume a paused service rate limit policy.

```bash
waygate srl enable <service>
```

---

### `waygate srl disable`

Pause the service rate limit without removing it. Per-route policies continue to enforce normally.

```bash
waygate srl disable <service>
```

---

## Audit log

### `waygate log`

Display the audit log, newest entries first. The `Status` column shows `old > new` for route state changes and a coloured action label for rate limit policy changes (including global RL actions such as `global set`, `global reset`, `global enabled`, `global disabled`, and service RL actions such as `svc set`, `svc reset`, `svc enabled`, `svc disabled`). The `Path` column shows human-readable labels for sentinel-keyed entries: `[Global Maintenance]`, `[Global Rate Limit]`, `[{service} Maintenance]`, and `[{service} Rate Limit]`.

```bash
waygate log                          # page 1, 20 rows
waygate log --route GET:/payments    # filter by route
waygate log --page 2                 # next page
waygate log --per-page 50           # 50 rows per page
```

| Option | Description |
|---|---|
| `--route ROUTE` | Filter entries to a single route key |
| `--page INT` | Page number to display (default: 1) |
| `--per-page INT` | Rows per page (default: 20) |

---

## Config commands

### `waygate config set-url`

Override the server URL and save it to `~/.waygate/config.json`. All subsequent commands will use this URL.

```bash
waygate config set-url http://prod.example.com/waygate
```

---

### `waygate config show`

Display the resolved server URL, its source (env var, `.waygate` file, or config file), and the current auth session status.

```bash
waygate config show
```

---

## Server URL discovery

The CLI resolves the server URL using the following priority order — highest wins:

| Priority | Source | Example |
|---|---|---|
| 1 (highest) | `WAYGATE_SERVER_URL` environment variable | `export WAYGATE_SERVER_URL=http://...` |
| 2 | `WAYGATE_SERVER_URL` in a `.waygate` file (walked up from the current directory) | `.waygate` file in project root |
| 3 | `server_url` in `~/.waygate/config.json` | Set via `waygate config set-url` |
| 4 (default) | Hard-coded default | `http://localhost:8000/waygate` |

!!! tip "Commit a `.waygate` file"
    Add a `.waygate` file to your project root so the whole team automatically uses the correct server URL without manual configuration:

    ```ini title=".waygate"
    WAYGATE_SERVER_URL=http://localhost:8000/waygate
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
waygate disable "GET:/payments"    # specific method
waygate enable "/payments"         # applies to all methods registered under /payments
```

---

## Token storage

Auth tokens are stored in a JSON file at a platform-specific location:

| Platform | Location |
|---|---|
| macOS / Linux | `~/.waygate/config.json` |
| Windows | `%USERPROFILE%\AppData\Local\waygate\config.json` |

The config file stores the server URL, the current token, the username, and the token expiry timestamp. Delete this file to clear all credentials.
