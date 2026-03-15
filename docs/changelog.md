# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.2.0] — Admin Redesign & Docs

### Added

#### Admin & Dashboard
- `ShieldAdmin` — unified admin interface combining the HTMX dashboard UI and the REST API under a single mount point
- REST API under `/api/` for programmatic route management (enable, disable, maintenance, schedule, audit, global maintenance)
- Token-based authentication with HMAC-SHA256 signing
- Support for single user, multiple users, and custom `ShieldAuthBackend` auth backends
- Automatic token invalidation when credentials change (auth fingerprint mixed into signing key)
- `HttpOnly` session cookies for dashboard browser sessions
- HTMX dashboard with SSE live updates, audit log table, and login page
- Platform field in audit log entries (`"cli"` / `"dashboard"`)

#### CLI
- `shield` CLI redesigned as a thin HTTP client over the `ShieldAdmin` REST API
- `shield login` / `shield logout`, `shield status`, `shield enable`, `shield disable`, `shield maintenance`, `shield schedule`, `shield log`
- `shield global` subcommands — enable, disable, status, exempt-add, exempt-remove
- `shield config set-url` and `shield config show`
- Cross-platform config file at `~/.shield/config.json`
- Server URL auto-discovery via `SHIELD_SERVER_URL` env var, `.shield` file, or config file

#### Documentation
- MkDocs Material documentation site with full tutorial, reference, guides, and adapter sections

### Changed
- CLI no longer accesses the backend directly — all operations go through the REST API
- Custom auth header changed from `Authorization: Bearer` to `X-Shield-Token`
- `ShieldDashboard` retained for backward compatibility; `ShieldAdmin` is now the recommended entry point

---

## [0.1.0] — Initial Release

### Added

#### Core
- `RouteStatus`, `MaintenanceWindow`, `RouteState`, `AuditEntry` Pydantic v2 models
- `ShieldException`, `MaintenanceException`, `EnvGatedException`, `RouteDisabledException`
- `ShieldBackend` ABC with full contract
- `MemoryBackend` — in-process dict with `asyncio.Queue` subscribe
- `FileBackend` — JSON file via `aiofiles` with `asyncio.Lock`
- `RedisBackend` — `redis-py` async with pub/sub for live dashboard updates in multi-instance deployments
- `ShieldEngine` — `check`, `register`, `enable`, `disable`, `set_maintenance`, `set_env_only`, `get_state`, `list_states`, `get_audit_log`
- Fail-open guarantee — backend errors are logged; requests pass through
- `MaintenanceScheduler` — `asyncio.Task`-based scheduler; auto-activates and auto-deactivates maintenance windows
- Webhook support — `add_webhook()` and `SlackWebhookFormatter`

#### FastAPI adapter
- `@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated` decorators
- `ShieldRouter` — drop-in `APIRouter` replacement with startup registration hook
- `ShieldMiddleware` — ASGI middleware with structured JSON error responses and `Retry-After` header
- `@deprecated` — injects `Deprecation`, `Sunset`, and `Link` response headers
- `apply_shield_to_openapi` — runtime OpenAPI schema filtering
- `setup_shield_docs` — enhanced `/docs` and `/redoc` with maintenance banners
- Global maintenance mode — `enable_global_maintenance`, `disable_global_maintenance`, `get_global_maintenance`

#### Dashboard (original)
- HTMX dashboard with SSE live updates and HTTP basic auth via `ShieldDashboard`
- Route list with status badges, enable/maintenance/disable actions per route
- Audit log table

#### CLI (original)
- `shield` CLI with direct backend access
- `shield status`, `shield enable`, `shield disable`, `shield maintenance`, `shield schedule`, `shield log`

[Unreleased]: https://github.com/Attakay78/api-shield/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Attakay78/api-shield/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Attakay78/api-shield/releases/tag/v0.1.0
