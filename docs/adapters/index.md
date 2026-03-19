# Framework Adapters

api-shield separates concerns cleanly:

- **`shield.core`** — the engine, backends, models, and exceptions. Zero framework imports. Works anywhere Python runs.
- **`shield.fastapi`** — the FastAPI adapter: ASGI middleware, route decorators, `ShieldRouter`, and OpenAPI integration.
- **`shield.<framework>`** — future adapters follow the same pattern.

---

## ASGI frameworks

api-shield is built on the **ASGI** standard with zero framework imports in `shield.core`. ASGI frameworks fall into two groups, each requiring a slightly different middleware approach:

**Starlette-based** — use `ShieldMiddleware` (`BaseHTTPMiddleware`) directly. These frameworks share Starlette's request/response model and middleware protocol.

**Pure ASGI** — frameworks like Quart and Django that implement the ASGI spec independently (no Starlette layer). Their adapters will use a raw ASGI callable (`async def __call__(scope, receive, send)`) so no Starlette dependency is introduced.

| Framework | Status | Adapter module | Middleware approach |
|---|---|---|---|
| **FastAPI** | ✅ Supported now | `shield.fastapi` | Starlette `BaseHTTPMiddleware` + ShieldRouter + OpenAPI integration |
| **Litestar** | 🔜 Planned | `shield.litestar` | Starlette-compatible middleware |
| **Starlette** | 🔜 Planned | `shield.starlette` | Starlette `BaseHTTPMiddleware` |
| **Quart** | 🔜 Planned | `shield.quart` | Pure ASGI callable (no Starlette dependency) |
| **Django (ASGI)** | 🔜 Planned | `shield.django` | Pure ASGI callable (no Starlette dependency) |

!!! note "Quart and Django ASGI"
    [Quart](https://quart.palletsprojects.com/) is the ASGI reimplementation of Flask and is a natural fit. [Django's ASGI mode](https://docs.djangoproject.com/en/stable/howto/deployment/asgi/) (`django.core.asgi`) makes Django routes available over ASGI. Neither uses Starlette internally, so their adapters will wrap the shield engine in a pure ASGI middleware layer — keeping `shield.quart` and `shield.django` free of any Starlette dependency.

See [**FastAPI Adapter**](fastapi.md) for the full guide on the currently supported adapter.

---

## WSGI frameworks

!!! warning "WSGI support is out of scope for this project"
    `api-shield` is an ASGI-native library. Integrating WSGI frameworks (Flask, Django, Bottle, …) via thread-bridging shims or a persistent background event loop would require a fundamentally different request model, and would introduce architectural compromises that undermine the reliability of both layers.

    **WSGI support will be delivered as a separate, dedicated project.** Building it from scratch for the synchronous request model — rather than patching it onto an async core — means both projects stay clean, well-tested, and maintainable without trade-offs.

    This is a deliberate design decision, not a gap. [Open an issue](https://github.com/Attakay78/api-shield/issues) or watch this repo to be notified when the WSGI companion project launches.

### Why not just add a sync bridge?

The short answer: it works until it doesn't, and the failure modes are silent.

A WSGI-to-ASGI bridge requires spawning a background asyncio event loop in a daemon thread and using `asyncio.run_coroutine_threadsafe()` to call into the async engine on every request. This creates several problems:

- **Connection pool fragmentation** — `RedisBackend` opens a connection pool tied to one event loop. Each WSGI worker process creates its own daemon loop, fragmenting the pool across processes with no shared state.
- **Thread-safety surface** — asyncio primitives (`asyncio.Lock`, `asyncio.Queue`) are not thread-safe. Wrapping them correctly across the WSGI/ASGI boundary requires significant additional machinery.
- **Testing complexity** — unit tests for sync WSGI views that touch the async engine require careful loop management, making the test suite fragile.
- **Hidden failures** — when the bridge breaks (deadlock, loop death, queue overflow), requests fail silently or block indefinitely rather than failing fast.

A purpose-built sync engine for WSGI avoids all of this.
