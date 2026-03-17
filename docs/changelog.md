# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Mobile & tablet responsive dashboard**: all four tables (Routes, Audit, Rate Limits, Blocked) transform into stacked cards on screens narrower than 640 px using a CSS-only card layout with `data-label` attributes. Action buttons collapse to icon-only on small screens.
- **Back-to-top button**: fixed button appears at the bottom-right after scrolling 200 px; hidden at the top.
- **Success toast notifications**: 2.5 s toast appears after any mutating action (enable, disable, maintenance, schedule, rate limit edit/reset/delete).

### Changed

- **`@env_only` now returns 403 with JSON**: env-gated routes blocked by the wrong environment return `403 ENV_GATED` with `current_env`, `allowed_envs`, and `path` instead of a silent empty 404.
- **Tailwind CSS v3 → v4**: replaced `tailwind.config.js` with a CSS-first config in `input.css` (`@import "tailwindcss"`, `@source`, `@theme`). Dashboard CSS is pre-built and committed; no Node.js required at install time.
- **No CDN dependency**: `shield.min.css` is now served as a local static file instead of the Tailwind CDN script, eliminating the production warning and removing the runtime network dependency.

---

## [0.3.0]

### Added

- **Rate limiting** (`@rate_limit`): per-IP, per-user, per-API-key, and global counters with fixed/sliding/moving window and token bucket algorithms. Supports burst allowance, tiered limits (`{"free": "10/min", "pro": "100/min"}`), exempt IPs/roles, and custom `on_missing_key` behaviour. Works as both a decorator and a `Depends()` dependency. Responses include `X-RateLimit-Limit/Remaining/Reset` and `Retry-After` headers. Requires `api-shield[rate-limit]`.
- **Rate limit custom responses**: `response=` on `@rate_limit` and `responses["rate_limited"]` on `ShieldMiddleware` for replacing the default 429 JSON body with any Starlette `Response`.
- **Rate limit dashboard**: `/shield/rate-limits` tab showing registered policies with reset/edit/delete actions; `/shield/blocked` page for the blocked requests log. Policies can also be managed via the `shield rl` CLI commands (`list`, `set`, `reset`, `delete`, `hits`).
- **Rate limit audit log**: policy changes (`set`, `update`, `reset`, `delete`) are recorded in the audit log alongside route state changes, with coloured action badges in the dashboard.
- **Rate limit storage**: `MemoryRateLimitStorage`, `FileRateLimitStorage` (in-memory counters with periodic disk snapshot), and `RedisRateLimitStorage` (atomic Redis counters, multi-worker safe). Storage is auto-selected based on the main backend.
- **Custom responses** for lifecycle decorators: `response=` on `@maintenance`, `@disabled`, and `@env_only`; `responses=` dict on `ShieldMiddleware` for `"maintenance"`, `"disabled"`, and `"env_gated"` states.
- **`@deprecated` as `Depends()`**: injects `Deprecation`, `Sunset`, and `Link` headers directly without requiring middleware.
- **Webhook deduplication**: `RedisBackend` uses `SET NX EX` to ensure only one instance fires webhooks per event in multi-instance deployments. Fails open on Redis error.
- **Distributed global maintenance**: `RedisBackend` publishes to `shield:global_invalidate` on every `set_global_config()` call so all instances drop their cached config immediately.
- **Distributed Deployments guide** (`docs/guides/distributed.md`): backend capability matrix, global maintenance cache invalidation, scheduler behaviour, webhook dedup, and production checklist.

### Changed

- Default `SHIELD_ENV` changed from `"production"` to `"dev"`. Set `SHIELD_ENV=production` explicitly in production deployments.
- `@deprecated` and all blocking decorators now support `Depends()` in addition to the decorator form.

---

## [0.2.0] — Admin Redesign & Docs

### Added

#### Admin & Dashboard
- `ShieldAdmin`: unified admin interface combining the HTMX dashboard UI and the REST API under a single mount point
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
- `shield global` subcommands: enable, disable, status, exempt-add, exempt-remove
- `shield config set-url` and `shield config show`
- Cross-platform config file at `~/.shield/config.json`
- Server URL auto-discovery via `SHIELD_SERVER_URL` env var, `.shield` file, or config file

#### Documentation
- MkDocs Material documentation site with full tutorial, reference, guides, and adapter sections

### Changed
- CLI no longer accesses the backend directly; all operations go through the REST API
- Custom auth header changed from `Authorization: Bearer` to `X-Shield-Token`
- `ShieldDashboard` retained for backward compatibility; `ShieldAdmin` is now the recommended entry point

---

## [0.1.0] — Initial Release

### Added

#### Core
- `RouteStatus`, `MaintenanceWindow`, `RouteState`, `AuditEntry` Pydantic v2 models
- `ShieldException`, `MaintenanceException`, `EnvGatedException`, `RouteDisabledException`
- `ShieldBackend` ABC with full contract
- `MemoryBackend`: in-process dict with `asyncio.Queue` subscribe
- `FileBackend`: JSON file via `aiofiles` with `asyncio.Lock`
- `RedisBackend`: `redis-py` async with pub/sub for live dashboard updates in multi-instance deployments
- `ShieldEngine`: `check`, `register`, `enable`, `disable`, `set_maintenance`, `set_env_only`, `get_state`, `list_states`, `get_audit_log`
- Fail-open guarantee: backend errors are logged; requests pass through
- `MaintenanceScheduler`: `asyncio.Task`-based scheduler that auto-activates and auto-deactivates maintenance windows
- Webhook support: `add_webhook()` and `SlackWebhookFormatter`

#### FastAPI adapter
- `@maintenance`, `@disabled`, `@env_only`, `@force_active`, `@deprecated` decorators
- `ShieldRouter`: drop-in `APIRouter` replacement with startup registration hook
- `ShieldMiddleware`: ASGI middleware with structured JSON error responses and `Retry-After` header
- `@deprecated`: injects `Deprecation`, `Sunset`, and `Link` response headers
- `apply_shield_to_openapi`: runtime OpenAPI schema filtering
- `setup_shield_docs`: enhanced `/docs` and `/redoc` with maintenance banners
- Global maintenance mode: `enable_global_maintenance`, `disable_global_maintenance`, `get_global_maintenance`

#### Dashboard (original)
- HTMX dashboard with SSE live updates and HTTP basic auth via `ShieldDashboard`
- Route list with status badges, enable/maintenance/disable actions per route
- Audit log table

#### CLI (original)
- `shield` CLI with direct backend access
- `shield status`, `shield enable`, `shield disable`, `shield maintenance`, `shield schedule`, `shield log`

[Unreleased]: https://github.com/Attakay78/api-shield/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Attakay78/api-shield/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Attakay78/api-shield/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Attakay78/api-shield/releases/tag/v0.1.0
