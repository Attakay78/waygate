"""Rate limiting data models for api-shield.

All models use Pydantic v2 (``model_validate``, ``model_dump``).

Key design notes
----------------
``RateLimitKeyStrategy`` has per-strategy defaults for ``on_missing_key``
behaviour documented inline — read the enum docstrings before choosing a
strategy.  Choosing the wrong strategy for a route can produce silent
mismeasurement in production.

``OnMissingKey`` controls what happens when the configured key strategy
cannot resolve a key for the current request.  The defaults are chosen
to be *safe, not silent*: a missing ``USER`` key exempts the request rather
than silently mixing authenticated and unauthenticated traffic in the same
counter.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RateLimitAlgorithm(StrEnum):
    """Rate limiting algorithm used to count and window requests.

    All algorithms are implemented by the ``limits`` library.
    api-shield never reimplements counting logic — this enum selects
    which ``limits`` strategy to apply.
    """

    FIXED_WINDOW = "fixed_window"
    """Count requests in fixed time buckets.

    **This is the default.**  Simple and predictable: allow N requests, then
    hard-block until the window resets.  One counter per window.

    Trade-off: allows bursts at window boundaries (up to 2× the limit in the
    worst case when requests cluster at the end of one window and the start of
    the next).  For most applications this is acceptable and the behaviour is
    exactly what users expect.
    """

    SLIDING_WINDOW = "sliding_window"
    """Approximate sliding window using two adjacent fixed-window counters.

    Smooths out boundary bursts at the cost of slightly higher memory.  Allows
    requests to trickle back in as old requests age out of the window — one slot
    opens roughly every ``period / limit`` seconds.

    **Use this when burst smoothing matters more than predictability.**
    Not recommended for small limits (e.g. ``5/minute``) where the gradual
    re-allow behaviour looks like intermittent blocking to clients.
    """

    MOVING_WINDOW = "moving_window"
    """Exact sliding window — timestamps every individual request.

    Most accurate; highest memory usage.  Use when burst accuracy matters
    more than memory.
    """

    TOKEN_BUCKET = "token_bucket"
    """Token bucket — tokens accumulate over time up to a capacity.

    Best for allowing short bursts while enforcing an average rate.

    Note: the ``limits`` library does not yet provide a native token-bucket
    strategy.  api-shield maps this to ``MOVING_WINDOW`` which provides
    comparable accuracy.  A future release will use a native implementation
    when ``limits`` ships one.
    """


class OnMissingKey(StrEnum):
    """Behaviour when a key strategy cannot resolve a key for a request.

    Applied per-request when the configured ``key_strategy`` returns ``None``.
    Each strategy has a **per-strategy default** (see ``RateLimitKeyStrategy``
    docstrings).  Set ``on_missing_key`` explicitly on a policy to override
    the default and make your intent clear.
    """

    EXEMPT = "exempt"
    """Skip the rate limit entirely.  Counter not incremented.

    The response is returned normally with **no** rate-limit headers.

    USE WHEN: the route requires auth and unauthenticated requests should
    not be rate limited by this decorator (let auth middleware handle them).
    """

    FALLBACK_IP = "fallback_ip"
    """Fall back to the client IP as the key.  Counter increments vs IP bucket.

    USE WHEN: you want to rate limit all callers but prefer per-identity
    limiting for authenticated ones (authenticated users get their own
    counter, unauthenticated requests share an IP counter).
    """

    BLOCK = "block"
    """Return 429 immediately without incrementing any counter.

    USE WHEN: the route must have a resolvable identity and you want a
    missing identity to be treated as a rate-limit violation.

    NOTE: If you want 401 / 403, use auth middleware — not this.
    BLOCK returns 429 to keep rate-limiter semantics consistent.
    """


class RateLimitKeyStrategy(StrEnum):
    """How to derive the per-request bucket key.

    **Each strategy has a documented ``on_missing_key`` default.**
    Read the docstring before choosing a strategy.  A misconfigured
    strategy produces silent mis-enforcement with no runtime error.
    """

    IP = "ip"
    """Extract client IP from headers / ASGI scope.

    **Never missing.** Falls back to ``"unknown"`` when no IP can be
    determined — this is the only strategy that always produces a key.
    Per-strategy ``on_missing_key`` default: N/A.
    """

    USER = "user"
    """Read ``request.state.user_id``.

    **Requires** your auth middleware to set ``request.state.user_id``
    *before* the rate limiter runs.  If it is not set, behaviour is
    controlled by ``on_missing_key``.

    Per-strategy default: ``EXEMPT``.

    ⚠️  Do **not** use on routes that allow unauthenticated access unless you
    explicitly set ``on_missing_key`` to declare what you want.
    """

    API_KEY = "api_key"
    """Read the ``X-API-Key`` request header.

    If the header is absent, behaviour is controlled by ``on_missing_key``.
    Per-strategy default: ``FALLBACK_IP`` — unauthenticated requests are
    still rate limited, just bucketed by IP rather than by key.
    """

    GLOBAL = "global"
    """Use the route path as the key.  Every caller shares one counter.

    **Never missing.**  Use for absolute throughput caps regardless of who
    is calling.
    """

    CUSTOM = "custom"
    """Caller provides an async callable ``(Request) -> str | None``.

    If the callable returns ``None``, behaviour is controlled by
    ``on_missing_key`` (default: ``EXEMPT``).  The callable must not raise —
    if it does, the exception propagates.
    """


# Per-strategy on_missing_key defaults.
# IP and GLOBAL are absent — they never produce a missing key.
STRATEGY_DEFAULTS: dict[RateLimitKeyStrategy, OnMissingKey] = {
    RateLimitKeyStrategy.USER: OnMissingKey.EXEMPT,
    RateLimitKeyStrategy.API_KEY: OnMissingKey.FALLBACK_IP,
    RateLimitKeyStrategy.CUSTOM: OnMissingKey.EXEMPT,
}


def resolve_on_missing_key(policy: RateLimitPolicy) -> OnMissingKey:
    """Return the effective ``OnMissingKey`` for *policy*.

    Uses the explicitly-set value when present, otherwise falls back to
    the per-strategy default from ``STRATEGY_DEFAULTS``.
    """
    if policy.on_missing_key is not None:
        return policy.on_missing_key
    return STRATEGY_DEFAULTS.get(policy.key_strategy, OnMissingKey.EXEMPT)


class RateLimitTier(BaseModel):
    """A named rate limit tier for tiered / SaaS-style limits.

    Parameters
    ----------
    name:
        Tier identifier, matched against ``request.state.<tier_resolver>``.
        E.g. ``"free"``, ``"pro"``, ``"enterprise"``.
    limit:
        Rate limit string in ``limits`` format.  Use ``"unlimited"`` to
        skip enforcement for this tier entirely.
    """

    name: str
    limit: str


class RateLimitPolicy(BaseModel):
    """Full rate limiting policy for a single route+method combination.

    Registered by ``ShieldRouter`` at startup and enforced by
    ``ShieldRateLimiter`` on every matching request.
    """

    path: str
    method: str
    limit: str
    """Rate limit in ``limits`` format, e.g. ``"100/minute"``, ``"1000/hour"``."""

    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW
    key_strategy: RateLimitKeyStrategy = RateLimitKeyStrategy.IP
    on_missing_key: OnMissingKey | None = None
    """When ``None``, the per-strategy default from ``STRATEGY_DEFAULTS`` is used."""

    burst: int = 0
    """Extra requests allowed above the base limit (additive)."""

    tiers: list[RateLimitTier] = Field(default_factory=list)
    """Tier-specific limit overrides.  When set, ``limit`` is the fallback."""

    tier_resolver: str = "plan"
    """``request.state`` attribute name used to look up the caller's tier."""

    exempt_ips: list[str] = Field(default_factory=list)
    """CIDR notation supported, e.g. ``"10.0.0.0/24"``."""

    exempt_roles: list[str] = Field(default_factory=list)
    """Matched against ``request.state.user_roles`` when set."""


class RateLimitResult(BaseModel):
    """Result of a single rate limit check.

    Always fully populated — never ``None``.  Middleware reads this to
    inject ``X-RateLimit-*`` response headers and handle 429 responses.
    """

    allowed: bool
    limit: str
    remaining: int
    reset_at: datetime
    retry_after_seconds: int
    """Only meaningful when ``allowed`` is ``False``."""

    key: str
    """The actual key used for the counter lookup."""

    tier: str | None = None
    """Which tier was applied, if any."""

    key_was_missing: bool = False
    """``True`` when ``on_missing_key`` behaviour fired for this request."""

    missing_key_behaviour: OnMissingKey | None = None
    """Which ``OnMissingKey`` behaviour fired, if any."""


class GlobalRateLimitPolicy(BaseModel):
    """Global rate limiting policy applied to all routes unless exempted.

    When set, every request that is not in ``exempt_routes`` is checked
    against this policy **in addition to** any per-route policy.

    The global check uses a dedicated storage namespace (``__global__``) so
    counters are independent of per-route counters.

    Parameters
    ----------
    limit:
        Rate limit in ``limits`` format, e.g. ``"1000/minute"``.
    algorithm:
        Algorithm to use for counting.  Defaults to FIXED_WINDOW.
    key_strategy:
        How to derive the per-request bucket key.  Defaults to IP.
    on_missing_key:
        What to do when the key strategy cannot produce a key.
        When ``None``, the per-strategy default from ``STRATEGY_DEFAULTS`` is used.
    burst:
        Extra requests allowed above the base limit (additive).
    exempt_routes:
        Routes that bypass the global limit.  Each entry is either a bare
        path (``"/health"``, all methods) or a method-prefixed path
        (``"GET:/api/internal"``).
    enabled:
        Whether the global limit is actively enforced.  Set to ``False``
        to temporarily disable without deleting the policy.
    """

    limit: str
    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW
    key_strategy: RateLimitKeyStrategy = RateLimitKeyStrategy.IP
    on_missing_key: OnMissingKey | None = None
    burst: int = 0
    exempt_routes: list[str] = Field(default_factory=list)
    enabled: bool = True


class RateLimitHit(BaseModel):
    """Record of a single blocked request.

    One entry is written to the backend for every request that exceeds the
    rate limit.  The log is capped at ``max_rl_hit_entries`` (default 10 000)
    — oldest entries are evicted when the cap is reached.
    """

    id: str
    """UUID4 identifier for this entry."""

    timestamp: datetime
    """When the request was blocked."""

    path: str
    method: str
    key: str
    limit: str
    tier: str | None = None
    reset_at: datetime
