# CLI

The `shield` CLI is a **thin HTTP client** — it talks to a running `ShieldAdmin` instance over HTTP. Install it separately if you only need the command-line tool:

```bash
uv add "api-shield[cli]"
```

---

## First-time setup

### 1. Start your app with ShieldAdmin mounted

```python
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

### 2. Configure the server URL

Drop a `.shield` file in your project root (commit it alongside your code so the whole team gets the right URL automatically):

```ini
# .shield
SHIELD_SERVER_URL=http://localhost:8000/shield
```

Or set it manually:

```bash
shield config set-url http://localhost:8000/shield
```

### 3. Log in

```bash
shield login admin
# Password: ••••••
```

Credentials are stored in `~/.shield/config.json` with an expiry timestamp.

---

## Route management

```bash
# Show all registered routes
shield status

# Inspect one route
shield status GET:/payments

# Enable a route
shield enable GET:/payments

# Disable permanently
shield disable GET:/payments --reason "Use /v2/payments instead"

# Put in maintenance (immediate)
shield maintenance GET:/payments --reason "DB migration"

# Put in maintenance with a window
shield maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z

# Schedule a future maintenance window (without activating now)
shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"

# Auto re-enable after a duration
shield disable GET:/payments --reason "hotfix" --until 2h
```

Sample `shield status` output:

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

## Global maintenance

```bash
# Enable global maintenance (blocks all non-exempt routes)
shield global enable --reason "Deploying v2"

# With exempt paths
shield global enable --reason "Deploying v2" --exempt /health --exempt GET:/status

# Block even @force_active routes
shield global enable --reason "Hard lockdown" --include-force-active

# Add/remove exemptions while active
shield global exempt-add /monitoring/ping
shield global exempt-remove /monitoring/ping

# Check current state
shield global status

# Restore normal operation
shield global disable
```

---

## Audit log

```bash
# Last 20 entries
shield log

# Filter by route
shield log --route GET:/payments

# More entries
shield log --limit 100
```

Sample output:

```
┌─────────────────────┬───────────────┬──────────┬──────────┬─────────────────────┐
│ Timestamp           │ Route         │ Action   │ Actor    │ Reason              │
├─────────────────────┼───────────────┼──────────┼──────────┼─────────────────────┤
│ 2025-06-01 02:00:01 │ GET:/payments │ maintain │ alice    │ DB migration        │
│ 2025-06-01 01:59:00 │ GET:/debug    │ disable  │ system   │                     │
└─────────────────────┴───────────────┴──────────┴──────────┴─────────────────────┘
```

---

## Auth commands

```bash
# Log in (prompts for password)
shield login admin

# Or inline (useful in CI)
shield login admin --password "$SHIELD_PASS"

# Check current session
shield config show

# Log out (revokes server-side token + clears local credentials)
shield logout
```

---

## Config commands

```bash
# Override server URL
shield config set-url http://prod.example.com/shield

# Show resolved URL + source + current session
shield config show
```

---

## Server URL discovery

The CLI resolves the server URL using this priority order (highest wins):

1. `SHIELD_SERVER_URL` environment variable
2. `SHIELD_SERVER_URL` in a `.shield` file (walks up from cwd)
3. `server_url` in `~/.shield/config.json`
4. Default: `http://localhost:8000/shield`

---

## Route key format

Routes are stored with method-prefixed keys:

| What you type | Stored as |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |

```bash
shield disable "GET:/payments"
shield enable "/payments"   # applies to all methods under /payments
```

---

## Next step

Dive into the full [**Reference documentation →**](../reference/decorators.md)
