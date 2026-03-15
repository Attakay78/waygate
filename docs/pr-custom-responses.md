# feat: custom responses for blocked routes

## Summary

- Added `response=` parameter to `@maintenance`, `@disabled`, and `@env_only` — pass a sync or async factory to return any response (HTML, redirect, plain text, custom JSON) instead of the default JSON error body
- Added `responses=` dict to `ShieldMiddleware` — set app-wide response defaults for all maintenance, disabled, or env-gated routes in one place
- Resolution order: per-route `response=` → global `responses[...]` → built-in JSON

## Why

The default JSON error body works well for pure API clients but falls short for apps that serve browser users. Teams needed a way to show a branded maintenance page, redirect to a status page, or return a custom error envelope — without forking the library or wrapping the middleware.

## How it works

**Per-route** — the response override lives next to the route definition:

```python
@router.get("/payments")
@maintenance(reason="DB migration — back at 04:00 UTC", response=maintenance_page)
async def payments(): ...
```

**Global default** — set once on the middleware, applies to every route without a per-route factory:

```python
app.add_middleware(
    ShieldMiddleware,
    engine=engine,
    responses={
        "maintenance": maintenance_page,
        "disabled": lambda req, exc: HTMLResponse("<h1>Gone</h1>", status_code=503),
    },
)
```

The factory signature is `(request: Request, exc: Exception) -> Response`. Both sync and async callables are supported, and any Starlette `Response` subclass is valid.

## Files changed

| File | Change |
|---|---|
| `shield/fastapi/decorators.py` | `response=` param on `@maintenance`, `@disabled`, `@env_only` |
| `shield/fastapi/middleware.py` | `responses=` dict on `ShieldMiddleware`; per-route → global → built-in resolution in `dispatch()` |
| `shield/fastapi/__init__.py` | `ResponseFactory` type alias exported |
| `examples/fastapi/custom_responses.py` | New runnable example covering all patterns |
| `docs/reference/decorators.md` | Per-route and global response docs with examples |
| `README.md` | Custom responses section updated |
| `docs/changelog.md` | Added to `[Unreleased]` |
