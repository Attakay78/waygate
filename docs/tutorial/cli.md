# CLI

The `waygate` CLI is a thin HTTP client that talks to a running `WaygateAdmin` instance over HTTP. Install it separately if you only need the command-line tool:

```bash
uv add "waygate[cli]"
```

---

## First-time setup

### 1. Start your app with WaygateAdmin mounted

```python title="main.py"
app.mount("/waygate", WaygateAdmin(engine=engine, auth=("admin", "secret")))
```

### 2. Configure the server URL

Drop a `.waygate` file in your project root (commit it alongside your code so the whole team gets the right URL automatically):

```ini title=".waygate"
WAYGATE_SERVER_URL=http://localhost:8000/waygate
```

Or set it manually:

```bash
waygate config set-url http://localhost:8000/waygate
```

### 3. Log in

```bash
waygate login admin
# Password: ••••••
```

Credentials are stored in `~/.waygate/config.json` with an expiry timestamp.

---

## Route management

```bash
waygate status                          # show all registered routes
waygate status GET:/payments            # inspect one route

waygate enable GET:/payments            # restore to ACTIVE
waygate disable GET:/payments --reason "Use /v2/payments instead"

waygate maintenance GET:/payments --reason "DB migration"

waygate maintenance GET:/payments \
  --reason "Planned migration" \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z

waygate schedule GET:/payments \
  --start 2025-06-01T02:00Z \
  --end   2025-06-01T04:00Z \
  --reason "Planned migration"

waygate disable GET:/payments --reason "hotfix" --until 2h
```

??? example "Sample `waygate status` output"

    | Route | Status | Reason | Since |
    |---|---|---|---|
    | GET /payments | MAINTENANCE | DB migration | 2 hours ago |
    | GET /debug | ENV_GATED | dev, staging only | startup |
    | GET /health | ACTIVE | | |

---

## Global maintenance

```bash
waygate global enable --reason "Deploying v2"

# Exempt specific paths so they keep responding
waygate global enable --reason "Deploying v2" --exempt /health --exempt GET:/status

# Block even @force_active routes
waygate global enable --reason "Hard lockdown" --include-force-active

# Adjust exemptions while active
waygate global exempt-add /monitoring/ping
waygate global exempt-remove /monitoring/ping

waygate global status    # check current state
waygate global disable   # restore normal operation
```

---

## Environment gating

Restrict a route to specific environments at runtime without redeploying.

```bash
waygate env set /api/debug dev                    # allow only the "dev" environment
waygate env set /api/internal dev staging         # allow dev and staging
waygate env clear /api/debug                      # remove the gate, restore to ACTIVE
```

!!! note
    The engine's `current_env` is set at startup (`WaygateEngine(current_env="prod")`). Requests from an environment not in `allowed_envs` receive a `403 ENV_GATED` response. `waygate env clear` is equivalent to calling `waygate enable` — it transitions the route back to `ACTIVE`.

---

## Multi-service context

When the Waygate Server manages multiple services, scope every command to the right service.

### Option A — `WAYGATE_SERVICE` env var (recommended)

```bash
export WAYGATE_SERVICE=payments-service
waygate status               # only payments-service routes
waygate disable GET:/payments --reason "hotfix"
waygate enable  GET:/payments
```

All route commands (`status`, `enable`, `disable`, `maintenance`, `schedule`) read `WAYGATE_SERVICE` automatically. An explicit `--service` flag always overrides it.

### Option B — `--service` flag per command

```bash
waygate status --service payments-service
waygate disable GET:/payments --service payments-service --reason "hotfix"
```

### Discover active context and connected services

```bash
waygate current-service          # show which service WAYGATE_SERVICE points to
waygate services                 # list all services registered with the Waygate Server
```

??? example "Sample `waygate services` output"

    ```
    Connected services
    ┌──────────────────────┐
    │ Service              │
    ├──────────────────────┤
    │ orders-service       │
    │ payments-service     │
    └──────────────────────┘
    ```

---

## Rate limits

Manage rate limit policies and view blocked requests. Requires `waygate[rate-limit]` on the server.

`waygate rl` and `waygate rate-limits` are aliases — use whichever you prefer.

```bash
waygate rl list                              # show all registered policies
waygate rl set GET:/public/posts 20/minute   # set or update a policy
waygate rl set GET:/search 5/minute --algorithm fixed_window --key global
waygate rl reset GET:/public/posts           # clear counters immediately
waygate rl delete GET:/public/posts          # remove persisted policy override
waygate rl hits                              # blocked requests log, page 1
waygate rl hits --page 2                     # next page
waygate rl hits --per-page 50               # 50 rows per page

# identical — waygate rate-limits is the full name
waygate rate-limits list
waygate rate-limits set GET:/public/posts 20/minute
```

!!! tip "SDK clients receive policy changes in real time"
    When using Waygate Server + WaygateSDK, rate limit policies set via `waygate rl set` are broadcast over the SSE stream and applied to every connected SDK client immediately — no restart required.

??? example "Sample `waygate rl list` output"

    | Route | Limit | Algorithm | Key Strategy |
    |---|---|---|---|
    | GET /public/posts | 10/minute | fixed_window | ip |
    | GET /search | 5/minute | fixed_window | global |
    | GET /users/me | 100/minute | fixed_window | user |

---

## Audit log

```bash
waygate log                          # page 1, 20 entries per page
waygate log --route GET:/payments    # filter by route
waygate log --page 2                 # next page
waygate log --per-page 50           # 50 rows per page
```

??? example "Sample `waygate log` output"

    | Timestamp | Route | Action | Actor | Platform | Status | Reason |
    |---|---|---|---|---|---|---|
    | 2025-06-01 02:00:01 | GET:/payments | maintenance | alice | cli | active > maintenance | DB migration |
    | 2025-06-01 01:59:00 | GET:/debug | disable | system | system | active > disabled | |
    | 2025-06-01 01:58:00 | GET:/payments | rl_policy_set | alice | cli | set | |

---

## Auth commands

```bash
waygate login admin                          # prompts for password interactively
waygate login admin --password "$WAYGATE_PASS"  # inline, useful in CI

waygate config show   # check current session and resolved URL
waygate logout        # revokes server-side token and clears local credentials
```

---

## Config commands

```bash
waygate config set-url http://prod.example.com/waygate   # override server URL
waygate config show                                      # show URL, source, session
```

---

## Server URL discovery

The CLI resolves the server URL using this priority order (highest wins):

| Priority | Source | How to set |
|---|---|---|
| 1 | `WAYGATE_SERVER_URL` environment variable | `export WAYGATE_SERVER_URL=http://...` |
| 2 | `WAYGATE_SERVER_URL` in a `.waygate` file (walked up from cwd) | Add to project root |
| 3 | `server_url` in `~/.waygate/config.json` | `waygate config set-url ...` |
| 4 | Default | `http://localhost:8000/waygate` |

---

## Route key format

Routes are identified by a method-prefixed key. Use the same format in all CLI commands:

| Decorator | Route key |
|---|---|
| `@router.get("/payments")` | `GET:/payments` |
| `@router.post("/payments")` | `POST:/payments` |
| `@router.get("/api/v1/users")` | `GET:/api/v1/users` |

```bash
waygate disable "GET:/payments"   # method-specific
waygate enable "/payments"        # applies to all methods under /payments
```

---

## Next step

Dive into the full [**Reference documentation →**](../reference/decorators.md)
