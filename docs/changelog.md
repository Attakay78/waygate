# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

#### Custom Responses
- `response=` parameter on `@maintenance`, `@disabled`, and `@env_only` decorators: pass a sync or async factory `(request, exc) -> Response` to replace the default JSON error body on a per-route basis
- `responses=` dict parameter on `ShieldMiddleware`: set app-wide response defaults for `"maintenance"`, `"disabled"`, and `"env_gated"` states without repeating the factory on every route
- Resolution order: per-route `response=` → global `responses[...]` → built-in JSON
- Factories receive the live `Request` and the triggering `ShieldException`, giving access to `exc.reason`, `exc.retry_after`, `request.url.path`, and any other request context
- Any Starlette `Response` subclass is supported: `HTMLResponse`, `JSONResponse`, `RedirectResponse`, `PlainTextResponse`, or a raw `Response`
- `ResponseFactory` type alias exported from `shield.fastapi` for use in type annotations
- `examples/fastapi/custom_responses.py`: runnable example demonstrating per-route and global response patterns

#### Webhooks
- `examples/fastapi/webhooks.py`: fully self-contained runnable example demonstrating all three webhook formatters (`default_formatter`, `SlackWebhookFormatter`, and a custom formatter) with in-app receivers and a live `/webhook-log` HTML viewer that auto-refreshes every 5 seconds

#### Dependency Injection
- `@deprecated` now works as a `Depends()` dependency, injecting `Deprecation`, `Sunset`, and `Link` response headers directly without requiring the middleware, using FastAPI's `Response` injection
- `_REQUEST_RESPONSE_SIGNATURE` added internally so FastAPI correctly injects both `Request` and `Response` into the deprecated dep
- `_ShieldCallable` extended with an optional `signature=` override and updated `__call__` to forward the `response` kwarg to `dep_raise` when present; fully backward compatible with all existing decorators
- `examples/fastapi/dependency_injection.py` updated to include `@deprecated` as a `Depends()` example and a clear explanation of why `@force_active` cannot be used as a dependency

#### Webhook Deduplication
- `ShieldBackend` ABC gains `try_claim_webhook_dispatch(dedup_key, ttl_seconds)`: returns `True` if this instance should fire webhooks, `False` if another instance already claimed the right for this event. Default implementation always returns `True` (single-instance backends never need dedup).
- `RedisBackend` overrides `try_claim_webhook_dispatch()` using `SET NX EX`: the first instance to win the atomic write fires webhooks; all others skip. Fails open: a Redis error returns `True` so webhooks are over-delivered rather than silently dropped.
- `ShieldEngine._fire_webhooks` refactored: now schedules a single `_dispatch_webhooks` task instead of one task per URL. The task computes a deterministic SHA-256 dedup key from `event + path + serialised RouteState`, claims dispatch rights via the backend, then fans out to individual webhook URLs only if the claim succeeds.
- Dedup key is deterministic across instances: because the scheduler produces an identical `RouteState` on all instances for the same window activation, the key is the same fleet-wide and only one instance wins.
- TTL on the dedup key defaults to 60 seconds; if the winning instance crashes mid-dispatch the key expires and re-delivery is possible on the next activation cycle.

#### Distributed Global Maintenance
- `RedisBackend` now publishes a lightweight invalidation signal to `shield:global_invalidate` whenever `set_global_config()` is called, so any other instance subscribed to this channel immediately drops its in-process `GlobalMaintenanceConfig` cache
- `ShieldBackend` ABC gains `subscribe_global_config()`: an async generator that yields `None` on each remote global config change; default implementation raises `NotImplementedError` (no-op for `MemoryBackend` and `FileBackend`)
- `ShieldEngine.start()`: starts a background `asyncio.Task` that listens for global config invalidation signals and calls `_invalidate_global_config_cache()` on each one; idempotent, safe to call multiple times
- `ShieldEngine.stop()`: cancels and awaits the listener task; called automatically by `__aexit__`
- `ShieldEngine.__aenter__` / `__aexit__` updated to call `start()` / `stop()` so CLI scripts using `async with ShieldEngine(...)` get distributed invalidation automatically
- `ShieldRouter.register_shield_routes()` calls `engine.start()` at application startup so FastAPI apps also start the listener without requiring the context manager
- For `MemoryBackend` / `FileBackend` the new code path is a transparent no-op: `NotImplementedError` is caught, the task exits immediately, and single-instance cache behaviour is unchanged

#### Documentation & Communication
- Early Access notice added to README and docs homepage, communicating that the library is fully functional and actively developed and inviting community feedback via GitHub Issues
- Webhooks and Custom Responses added to the Key Features table in the docs homepage
- Key Features section added to `README.md`
- New guide: **Distributed Deployments** (`docs/guides/distributed.md`): covers backend capability matrix, the request lifecycle across instances, global maintenance cache invalidation architecture, scheduler behaviour and webhook deduplication in multi-instance setups, OpenAPI schema staleness, the fail-open guarantee, and a production checklist. Explains why `FileBackend` intentionally does not support cross-instance sync and when to use each backend.

### Changed
- `@deprecated` docstring updated: no longer described as decorator-only; documents the `Depends()` usage pattern
- `@force_active` docstring updated: clearly explains why it cannot be a `Depends()` (middleware completes before dependencies are resolved)
- **Default `SHIELD_ENV` changed from `"production"` to `"dev"`**: `ShieldEngine`, `make_engine()`, and all examples now default to the `dev` environment. Set `SHIELD_ENV=production` (or pass `current_env=` explicitly) in production deployments. This makes the out-of-the-box experience work correctly for local development where `@env_only("dev")` routes should be accessible by default.

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

[Unreleased]: https://github.com/Attakay78/api-shield/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Attakay78/api-shield/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Attakay78/api-shield/releases/tag/v0.1.0
