# CLI

The `shield` CLI is a thin HTTP client that talks to a running `ShieldAdmin` instance over HTTP. Install it separately if you only need the command-line tool:

```bash
uv add "api-shield[cli]"
```

---

## First-time setup

### 1. Start your app with ShieldAdmin mounted

```python title="main.py"
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

### 2. Configure the server URL

Drop a `.shield` file in your project root (commit it alongside your code so the whole team gets the right URL automatically):

```ini title=".shield"
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
shield status                          # show all registered routes
shield status GET:/payments            # inspect one route

shield enable GET:/payments            # restore to ACTIVE
shield disable GET:/payments --reason "Use /v2/payments instead"

shield maintenance GET:/payments --reason "DB migration"

shield maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z

shield schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"

shield disable GET:/payments --reason "hotfix" --until 2h
```

??? example "Sample `shield status` output"

    | Route | Status | Reason | Since |
    |---|---|---|---|
    | GET /payments | MAINTENANCE | DB migration | 2 hours ago |
    | GET /debug | ENV_GATED | dev, staging only | startup |
    | GET /health | ACTIVE | | |

---

## Global maintenance

```bash
shield global enable --reason "Deploying v2"

# Exempt specific paths so they keep responding
shield global enable --reason "Deploying v2" --exempt /health --exempt GET:/status

# Block even @force_active routes
shield global enable --reason "Hard lockdown" --include-force-active

# Adjust exemptions while active
shield global exempt-add /monitoring/ping
shield global exempt-remove /monitoring/ping

shield global status    # check current state
shield global disable   # restore normal operation
```

---

## Audit log

```bash
shield log                          # last 20 entries
shield log --route GET:/payments    # filter by route
shield log --limit 100              # show more entries
```

??? example "Sample `shield log` output"

    | Timestamp | Route | Action | Actor | Platform | Reason |
    |---|---|---|---|---|---|
    | 2025-06-01 02:00:01 | GET:/payments | maintenance | alice | cli | DB migration |
    | 2025-06-01 01:59:00 | GET:/debug | disable | system | system | |

---

## Auth commands

```bash
shield login admin                          # prompts for password interactively
shield login admin --password "$SHIELD_PASS"  # inline, useful in CI

shield config show   # check current session and resolved URL
shield logout        # revokes server-side token and clears local credentials
```

---

## Config commands

```bash
shield config set-url http://prod.example.com/shield   # override server URL
shield config show                                      # show URL, source, session
```

---

## Server URL discovery

The CLI resolves the server URL using this priority order (highest wins):

| Priority | Source | How to set |
|---|---|---|
| 1 | `SHIELD_SERVER_URL` environment variable | `export SHIELD_SERVER_URL=http://...` |
| 2 | `SHIELD_SERVER_URL` in a `.shield` file (walked up from cwd) | Add to project root |
| 3 | `server_url` in `~/.shield/config.json` | `shield config set-url ...` |
| 4 | Default | `http://localhost:8000/shield` |

---

## Route key format

Routes are identified by a method-prefixed key. Use the same format in all CLI commands:

| Decorator | Route key |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |
| `@router.get("/api/v1/users")` | `GET:/api/v1/users` |

```bash
shield disable "GET:/payments"   # method-specific
shield enable "/payments"        # applies to all methods under /payments
```

---

## Next step

Dive into the full [**Reference documentation →**](../reference/decorators.md)
