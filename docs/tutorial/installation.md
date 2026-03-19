# Installation

## Requirements

- Python **3.11** or higher
- An ASGI web framework (FastAPI is currently supported; Starlette and other ASGI frameworks are on the roadmap)

---

## Install with uv (recommended)

```bash
# Minimal — core library only (no framework adapter, no CLI, no dashboard)
uv add api-shield

# FastAPI adapter
uv add "api-shield[fastapi]"

# FastAPI + CLI
uv add "api-shield[fastapi,cli]"

# FastAPI + rate limiting
uv add "api-shield[fastapi,rate-limit]"

# Everything (FastAPI adapter, Redis, dashboard, CLI, admin, rate limiting)
uv add "api-shield[all]"
```

## Install with pip

```bash
pip install "api-shield[all]"
```

---

## Optional extras

| Extra | What it adds | When to use |
|---|---|---|
| `fastapi` | FastAPI adapter (middleware, decorators, router, OpenAPI integration) | FastAPI apps |
| `redis` | `RedisBackend` for multi-instance deployments | Production with multiple replicas |
| `dashboard` | Jinja2 + aiofiles for the HTMX dashboard | When mounting the admin UI |
| `admin` | Unified `ShieldAdmin` (dashboard + REST API) | Recommended for CLI support |
| `cli` | `shield` command-line tool + httpx client | Operators managing routes from the terminal |
| `rate-limit` | `limits` library for `@rate_limit` enforcement | Any app using rate limiting |
| `all` | All of the above | Easiest option for most projects |

---

## Verify the installation

```bash
# Check the library is importable
python -c "import shield; print(shield.__version__)"

# Check the CLI is available
shield --help
```

---

## Environment variables

api-shield can be configured through environment variables so no code changes are needed between environments:

| Variable | Default | Description |
|---|---|---|
| `SHIELD_BACKEND` | `memory` | Backend type: `memory`, `file`, or `redis` |
| `SHIELD_ENV` | `dev` | Current environment name (used by `@env_only`) |
| `SHIELD_FILE_PATH` | `shield-state.json` | Path for `FileBackend` |
| `SHIELD_REDIS_URL` | `redis://localhost:6379/0` | URL for `RedisBackend` |

Or commit a `.shield` file in your project root; both the app and the CLI discover it automatically:

```ini
# .shield
SHIELD_BACKEND=file
SHIELD_FILE_PATH=shield-state.json
SHIELD_ENV=dev
SHIELD_SERVER_URL=http://localhost:8000/shield
```

---

## Next step

[**Tutorial: Your first decorator →**](first-decorator.md)
