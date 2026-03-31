"""Rate limiting core for waygate.

Optional feature — requires ``pip install waygate[rate-limit]``.

Modules
-------
models   — Pydantic models: RateLimitPolicy, RateLimitResult, etc.
storage  — Storage bridge wrapping the ``limits`` library backends.
keys     — Key extraction from requests (IP, user, API key, custom).
limiter  — WaygateRateLimiter orchestrating checks against policies.
"""
