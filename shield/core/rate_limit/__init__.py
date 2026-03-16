"""Rate limiting core for api-shield.

Optional feature — requires ``pip install api-shield[rate-limit]``.

Modules
-------
models   — Pydantic models: RateLimitPolicy, RateLimitResult, etc.
storage  — Storage bridge wrapping the ``limits`` library backends.
keys     — Key extraction from requests (IP, user, API key, custom).
limiter  — ShieldRateLimiter orchestrating checks against policies.
"""
