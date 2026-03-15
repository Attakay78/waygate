# CLI Reference

The `shield` CLI is a thin HTTP client. It communicates with a running `ShieldAdmin` instance over HTTP — it does not access the backend directly.

```bash
uv add "api-shield[cli]"
```

---

## Auth commands

### `shield login`

Authenticate with a ShieldAdmin server and store the token locally.

```bash
shield login <username>
shield login admin                     # prompts for password
shield login admin --password secret   # inline (useful in CI)
```

Tokens are stored in `~/.shield/config.json` with an expiry timestamp.

### `shield logout`

Revoke the server-side token and clear local credentials.

```bash
shield logout
```

---

## Route commands

### `shield status`

Show all registered routes or inspect one route.

```bash
shield status                           # all routes
shield status GET:/payments             # one route
```

Output:

```
┌─────────────────────┬─────────────┬──────────────────────┬──────────────┐
│ Route               │ Status      │ Reason               │ Since        │
├─────────────────────┼─────────────┼──────────────────────┼──────────────┤
│ GET /payments       │ MAINTENANCE │ DB migration         │ 2 hours ago  │
│ GET /debug          │ ENV_GATED   │ dev, staging only    │ startup      │
│ GET /health         │ ACTIVE      │                      │              │
└─────────────────────┴─────────────┴──────────────────────┴──────────────┘
```

### `shield enable`

Restore a route to `ACTIVE`.

```bash
shield enable GET:/payments
```

### `shield disable`

Permanently disable a route.

```bash
shield disable GET:/payments
shield disable GET:/payments --reason "Use /v2/payments instead"
shield disable GET:/payments --reason "hotfix" --until 2h    # auto re-enable after duration
```

| Option | Description |
|---|---|
| `--reason TEXT` | Reason shown in error responses and audit log |
| `--until DURATION` | Auto re-enable after this duration (e.g. `2h`, `30m`, `1d`) |

### `shield maintenance`

Put a route in maintenance mode.

```bash
shield maintenance GET:/payments --reason "DB swap"

# With a scheduled window
shield maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z
```

| Option | Description |
|---|---|
| `--reason TEXT` | Reason shown in error responses |
| `--start DATETIME` | Start of maintenance window (ISO 8601) |
| `--end DATETIME` | End of maintenance window — sets `Retry-After` header |

### `shield schedule`

Schedule a future maintenance window without activating maintenance now.

```bash
shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"
```

---

## Global maintenance commands

### `shield global status`

Show the current global maintenance state.

```bash
shield global status
```

### `shield global enable`

Block all non-exempt routes immediately.

```bash
shield global enable --reason "Deploying v2"
shield global enable --reason "Deploying v2" --exempt /health --exempt GET:/status
shield global enable --reason "Hard lockdown" --include-force-active
```

| Option | Description |
|---|---|
| `--reason TEXT` | Shown in every 503 response |
| `--exempt PATH` | Exempt a path (repeatable). Bare (`/health`) or method-prefixed (`GET:/health`) |
| `--include-force-active` | Block `@force_active` routes too |

### `shield global disable`

Restore all routes to their individual states.

```bash
shield global disable
```

### `shield global exempt-add`

Add a path exemption while global maintenance is active.

```bash
shield global exempt-add /monitoring/ping
```

### `shield global exempt-remove`

Remove a path exemption.

```bash
shield global exempt-remove /monitoring/ping
```

---

## Audit log

### `shield log`

Display the audit log.

```bash
shield log                          # last 20 entries
shield log --route GET:/payments    # filter by route
shield log --limit 100
```

---

## Config commands

### `shield config set-url`

Override the server URL (stored in `~/.shield/config.json`).

```bash
shield config set-url http://prod.example.com/shield
```

### `shield config show`

Display the resolved server URL, its source, and the current auth session.

```bash
shield config show
```

---

## Server URL discovery

The CLI resolves the server URL in this priority order (highest wins):

1. `SHIELD_SERVER_URL` environment variable
2. `SHIELD_SERVER_URL` in a `.shield` file (walked up from current directory)
3. `server_url` in `~/.shield/config.json`
4. Default: `http://localhost:8000/shield`

Commit a `.shield` file in your project root so the whole team uses the correct URL automatically:

```ini
# .shield
SHIELD_SERVER_URL=http://localhost:8000/shield
```

---

## Route key format

Routes are stored with method-prefixed keys. Use the same format in CLI commands:

| Decorator | Route key |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |
| `@router.get("/api/v1/users")` | `GET:/api/v1/users` |

```bash
shield disable "GET:/payments"
shield enable "/payments"    # applies to all methods under /payments
```
