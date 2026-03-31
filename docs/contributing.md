# Contributing

Contributions are welcome with bug fixes, new features, documentation improvements, and adapter implementations all help make waygate better. This page walks you through everything you need to get started.

---

## Before you start

- Check the [issue tracker](https://github.com/Attakay78/waygate/issues) to see if someone is already working on the same thing.
- For significant changes, open an issue first so we can align on the approach before you invest time writing code.
- All PRs target the `develop` branch, not `main`.

---

## Setting up your development environment

### 1. Python environment

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/Attakay78/waygate
cd waygate

# Create a virtual environment and install all extras + dev tools
uv venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
uv pip install -e ".[all,dev]"

# Wire up the pre-commit hooks (runs ruff automatically on every commit)
pre-commit install
```

### 2. Dashboard CSS (Tailwind)

The admin dashboard is styled with [Tailwind CSS v4](https://tailwindcss.com/). The compiled stylesheet (`waygate/dashboard/static/waygate.min.css`) is **committed to the repository** so that `pip install waygate` works without requiring Node.js on the user's machine.

Configuration lives entirely in `input.css` via `@theme` and `@source` directives — there is no `tailwind.config.js` in v4.

**Requirements:** Node.js 18+

```bash
# Install Tailwind (one-time setup after cloning)
npm install
```

Two npm scripts are available:

| Command | When to use |
|---|---|
| `npm run build:css` | One-shot rebuild — run before committing template changes |
| `npm run watch:css` | Continuous rebuild — run while actively editing templates |

#### When you must rebuild

You need to rebuild and commit `waygate.min.css` whenever you:

- Add or change Tailwind utility classes in any file under `waygate/dashboard/templates/`
- Create a new template file
- Modify `input.css` (custom breakpoints, colours, or font config)

```bash
# Edit templates, then:
npm run build:css
git add waygate/dashboard/static/waygate.min.css
git commit -m "rebuild: update waygate.min.css"
```

!!! warning "CI enforces this"
    The `css` CI job rebuilds the stylesheet from scratch and fails the PR if
    `waygate.min.css` does not match the current templates. A forgotten rebuild will
    block the merge.

---

## Branching strategy

| Branch | Purpose |
|---|---|
| `main` | Stable releases only, never commit directly |
| `develop` | Integration branch, all PRs target here |

```
feat/my-feature  →  develop  →  (release PR)  →  main  →  vX.Y.Z tag
```

```bash
git checkout develop
git pull
git checkout -b feat/my-feature
```

---

## Commit messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(core): add rollout percentage to WaygateEngine
fix(middleware): handle missing path in check()
docs: add Redis backend guide
chore(ci): pin ruff to v0.9
rebuild: update waygate.min.css
```

---

## Running tests

```bash
# All tests — Redis tests auto-skip when Redis is unavailable
pytest

# Narrow to a module
pytest tests/core/
pytest tests/fastapi/
pytest tests/dashboard/

# Run with a real Redis instance
WAYGATE_REDIS_URL=redis://localhost:6379 pytest
```

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`, all async test functions work without decorators.

### Testing conventions

- Core tests (`tests/core/`) must never import `fastapi` or `starlette`.
- FastAPI tests use `httpx.AsyncClient` with `ASGITransport`, no live server.
- CLI tests use `typer.testing.CliRunner` and must be **sync `def`**, not `async def`, because the CLI calls `anyio.run()` internally.
- Backend tests are parametrized over all three backends (`memory`, `file`, `redis`).

---

## Linting & formatting

We use [ruff](https://docs.astral.sh/ruff/) for both linting and formatting:

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
ruff format --check . # check formatting without modifying files
```

Pre-commit runs ruff automatically on staged files before each commit. CI will fail on any ruff errors or formatting drift.

---

## Architecture rules

These constraints are enforced at review time. PRs that violate them will be asked to refactor before merging:

1. **`waygate.core` has zero framework imports.**
   It must never import from `waygate.fastapi`, `waygate.dashboard`, or `waygate.cli`. Core is the dependency and everything else depends on it.

2. **All business logic lives in `WaygateEngine`.**
   Middleware and decorators are transport layers. They call engine methods; they never make state decisions themselves.

3. **Decorators only stamp metadata.**
   `@maintenance(...)` attaches `__waygate_meta__` to the function and does nothing else. `WaygateRouter` reads this at startup. The decorator wrapper never executes logic at request time.

4. **`engine.check()` is the single chokepoint.**
   Every request path must flow through `engine.check()`. Never duplicate the check logic in middleware, a dependency, or a decorator.

5. **Backends implement the full `WaygateBackend` ABC.**
   No partial implementations. If a method is not supported (e.g. `subscribe()` on `FileBackend`), it raises `NotImplementedError`. `WaygateEngine.start()` catches this internally and skips the listener — the engine handles the fallback, not the caller.

6. **Fail-open on backend errors.**
   If `backend.get_state()` raises, `engine.check()` logs the error and lets the request through. Waygate must never take down an API because its own storage is temporarily unavailable.

---

## CI jobs

| Job | What it checks |
|---|---|
| `css` | Rebuilds `waygate.min.css` and asserts it matches the committed file |
| `lint` | `ruff check` + `ruff format --check` + `mypy --strict` |
| `test` | Full pytest suite on Python 3.11 / 3.12 / 3.13 × Linux / macOS / Windows |
| `test-redis` | Pytest suite against a live Redis 7 instance |

All four jobs must pass before a PR can be merged to `develop`.

---

## Project structure (quick reference)

```
waygate/
├── core/               # Zero framework dependencies — engine, models, backends
│   ├── engine.py       # WaygateEngine — all business logic lives here
│   ├── models.py       # RouteState, AuditEntry, RateLimitPolicy, …
│   ├── backends/       # MemoryBackend, FileBackend, RedisBackend, WaygateServerBackend
│   ├── rate_limit/     # Rate limiting subsystem
│   └── scheduler.py    # asyncio-based maintenance window scheduler
├── fastapi/            # FastAPI adapter — middleware, decorators, router, OpenAPI
├── admin/              # Unified admin ASGI app (dashboard UI + REST API + auth)
├── server/             # WaygateServer — standalone control plane for multi-service deployments
├── sdk/                # WaygateSDK — service-side client that connects to a Waygate Server via SSE
├── adapters/           # Framework adapter helpers (ASGI base, future adapter scaffolding)
├── dashboard/          # HTMX/Jinja2 templates and static assets
│   ├── templates/      # Edit these, then run `npm run build:css`
│   └── static/         # waygate.min.css lives here — commit after rebuilding
└── cli/                # Typer CLI — thin HTTP client that talks to WaygateAdmin
```
