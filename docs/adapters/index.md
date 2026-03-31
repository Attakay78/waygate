# Framework Adapters

waygate separates concerns cleanly:

- **`waygate.core`** — the engine, backends, models, and exceptions. Zero framework imports. Works anywhere Python runs.
- **`waygate.fastapi`** — the FastAPI adapter: ASGI middleware, route decorators, `WaygateRouter`, and OpenAPI integration.
- **`waygate.<framework>`** — future adapters follow the same pattern.

---

## Supported frameworks

We currently support **FastAPI**. More framework adapters are on the way.

| Framework | Status | Adapter |
|---|---|---|
| **FastAPI** | ✅ Supported | `waygate.fastapi` |
| More coming | 🔜 On the way | — |

See [**FastAPI Adapter**](fastapi.md) for the full guide.

> Want your framework supported? [Open an issue](https://github.com/Attakay78/waygate/issues).
