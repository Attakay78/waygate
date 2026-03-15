<div align="center">
  <img src="api-shield-logo.svg" alt="API Shield" width="600"/>

  <p><strong>Route lifecycle management for Python web frameworks — maintenance mode, environment gating, deprecation, admin panels, and more. No restarts required.</strong></p>

  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi&cacheSeconds=300" alt="PyPI"></a>
  <a href="https://pypi.org/project/api-shield"><img src="https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B" alt="License"></a>
</div>

---

> [!WARNING]
> **Early Access** — `api-shield` is fully functional and ready to use. We are actively building on top of a solid foundation and your real-world experience is invaluable at this stage. If you have feedback, feature ideas, or suggestions, please [open an issue](https://github.com/Attakay78/api-shield/issues) — every voice helps shape the roadmap.

---

## Install

```bash
uv add "api-shield[all]"
# or: pip install "api-shield[all]"
```

## Quickstart

```python
from fastapi import FastAPI
from shield.core.config import make_engine
from shield.fastapi import (
    ShieldMiddleware, ShieldAdmin, apply_shield_to_openapi,
    maintenance, env_only, disabled, force_active, deprecated,
)

engine = make_engine()

app = FastAPI()
app.add_middleware(ShieldMiddleware, engine=engine)

@app.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC")
async def get_payments():
    return {"payments": []}

@app.get("/health")
@force_active
async def health():
    return {"status": "ok"}

apply_shield_to_openapi(app, engine)
app.mount("/shield", ShieldAdmin(engine=engine, auth=("admin", "secret")))
```

```
GET /payments  → 503  {"error": {"code": "MAINTENANCE_MODE", ...}}
GET /health    → 200  always
```

Manage routes from the CLI — no code changes, no restarts:

```bash
shield config set-url http://localhost:8000/shield
shield login admin
shield status
shield enable GET:/payments
shield global enable --reason "Deploying v2" --exempt /health
```

## Decorators

| Decorator | Effect | Status |
|---|---|---|
| `@maintenance(reason, start, end)` | Temporarily unavailable | 503 |
| `@disabled(reason)` | Permanently off | 503 |
| `@env_only("dev", "staging")` | Restricted to named environments | 404 elsewhere |
| `@deprecated(sunset, use_instead)` | Still works, injects deprecation headers | 200 |
| `@force_active` | Bypasses all shield checks | Always 200 |

## Backends

| Backend | Persistence | Multi-instance |
|---|---|---|
| `MemoryBackend` | No | No |
| `FileBackend` | Yes | No |
| `RedisBackend` | Yes | Yes |

---

## Documentation

Full documentation at **[attakay78.github.io/api-shield](https://attakay78.github.io/api-shield)**

| | |
|---|---|
| [Tutorial](https://attakay78.github.io/api-shield/tutorial/installation/) | Get started in 5 minutes |
| [Decorators reference](https://attakay78.github.io/api-shield/reference/decorators/) | All decorator options |
| [ShieldEngine reference](https://attakay78.github.io/api-shield/reference/engine/) | Programmatic control |
| [Backends](https://attakay78.github.io/api-shield/tutorial/backends/) | Memory, File, Redis, custom |
| [Admin dashboard](https://attakay78.github.io/api-shield/tutorial/admin-dashboard/) | Mounting ShieldAdmin |
| [CLI reference](https://attakay78.github.io/api-shield/reference/cli/) | All CLI commands |
| [Production guide](https://attakay78.github.io/api-shield/guides/production/) | Monitoring & deployment automation |

## License

[MIT](LICENSE)
