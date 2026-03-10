# Contributing to api-shield

Thank you for considering a contribution! This document covers how to get set up, the branching strategy, and the standards we hold all code to.

---

## Development setup

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/Attakay78/api-shield
cd api-shield

# Create venv and install all dependencies
uv venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[all,dev]"

# Install pre-commit hooks (runs ruff on every commit)
pre-commit install
```

---

## Branching & git flow

| Branch | Purpose |
|---|---|
| `main` | Stable, tagged releases only. Never commit directly. |
| `develop` | Integration branch. All PRs target this branch. |
| `feat/<name>` | New features |
| `fix/<name>` | Bug fixes |
| `chore/<name>` | Tooling, CI, dependency updates |
| `docs/<name>` | Documentation only |

**Workflow:**

```
feat/my-feature  →  develop  →  (release PR)  →  main  →  vX.Y.Z tag
```

1. Branch off `develop`:
   ```bash
   git checkout develop
   git pull
   git checkout -b feat/my-feature
   ```

2. Make changes, commit using the [Conventional Commits](https://www.conventionalcommits.org/) format:
   ```
   feat(core): add rollout percentage to ShieldEngine
   fix(middleware): handle missing path in check()
   docs: add Redis backend guide
   chore(ci): pin ruff to v0.9
   ```

3. Push and open a PR against `develop`.

---

## Running tests

```bash
# All tests (Redis tests auto-skip when Redis is not running)
pytest

# Specific module
pytest tests/core/
pytest tests/fastapi/

# With Redis (start Redis first)
SHIELD_REDIS_URL=redis://localhost:6379 pytest
```

---

## Linting & formatting

We use [ruff](https://docs.astral.sh/ruff/) for both linting and formatting. Pre-commit runs it automatically on staged files, but you can also run it manually:

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
```

CI will fail on any ruff errors or formatting drift.

---

## Architecture rules

These are hard constraints enforced by the project design. PRs that violate them will not be merged:

1. **`shield.core` must never import from `shield.fastapi`, `shield.dashboard`, or `shield.cli`.**
2. **All business logic lives in `ShieldEngine`.** Middleware and decorators are transport layers only.
3. **Decorators stamp `__shield_meta__` and do nothing else** — no logic at request time.
4. **`engine.check()` is the single chokepoint** — never duplicate the check logic elsewhere.
5. **Backends must implement the full `ShieldBackend` ABC** — no partial implementations.
6. **Fail-open** — if the backend is unreachable, the request passes through. Shield never takes down an API.

