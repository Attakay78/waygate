"""WaygateRateLimiter — orchestrates rate limit checks against policies.

The limiter is the single entry point for rate limit enforcement.
``WaygateEngine.check()`` calls ``WaygateRateLimiter.check()`` when a policy
is registered for the route being checked.

Algorithm
---------
1. Check exemptions (IP CIDR, role-based) — exempt → allow immediately.
2. Resolve the effective limit (tier override or base limit).
3. ``"unlimited"`` tier → allow immediately without touching storage.
4. Extract the key using the configured ``KeyExtractor``.
5. Handle missing key (``on_missing_key`` logic) when extractor returns ``None``.
6. Build the namespaced storage key.
7. Call ``storage.increment()`` and return the ``RateLimitResult``.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request

from waygate.core.rate_limit.keys import (
    KeyExtractor,
    handle_missing_key,
    is_exempt,
    resolve_key_extractor,
)
from waygate.core.rate_limit.models import (
    OnMissingKey,
    RateLimitAlgorithm,
    RateLimitPolicy,
    RateLimitResult,
)
from waygate.core.rate_limit.storage import RateLimitStorage

logger = logging.getLogger(__name__)

# Prefix used for all rate limit storage keys to avoid collisions.
_KEY_PREFIX = "waygate:ratelimit"


class WaygateRateLimiter:
    """Orchestrates rate limit checks against registered policies.

    Parameters
    ----------
    storage:
        The rate limit counter storage implementation.
    default_algorithm:
        Algorithm used when a policy does not specify one.
    """

    def __init__(
        self,
        storage: RateLimitStorage,
        default_algorithm: RateLimitAlgorithm = RateLimitAlgorithm.FIXED_WINDOW,
    ) -> None:
        self._storage = storage
        self._default_algorithm = default_algorithm
        # Cache custom key extractors so callables aren't re-wrapped each request.
        self._extractor_cache: dict[str, KeyExtractor] = {}

    def _get_extractor(self, policy: RateLimitPolicy, custom_func: Any = None) -> KeyExtractor:
        """Return (and cache) the key extractor for the given policy."""
        cache_key = f"{policy.path}:{policy.method}:{policy.key_strategy}"
        if cache_key not in self._extractor_cache:
            self._extractor_cache[cache_key] = resolve_key_extractor(
                policy.key_strategy,
                custom_func=custom_func,
            )
        return self._extractor_cache[cache_key]

    def _resolve_limit(self, policy: RateLimitPolicy, request: Request) -> tuple[str, str | None]:
        """Return ``(effective_limit_str, tier_name_or_None)``.

        If ``policy.tiers`` is set, reads ``request.state.<tier_resolver>`` to
        find the caller's tier.  Falls back to ``policy.limit`` when the tier
        attribute is missing or the tier name doesn't match any configured tier.
        """
        if not policy.tiers:
            return policy.limit, None

        tier_name: str | None = getattr(request.state, policy.tier_resolver, None)
        if tier_name:
            for tier in policy.tiers:
                if tier.name == tier_name:
                    return tier.limit, tier.name

        return policy.limit, None

    async def check(
        self,
        path: str,
        method: str,
        request: Request | None,
        policy: RateLimitPolicy,
        custom_key_func: Any = None,
    ) -> RateLimitResult:
        """Check and increment the rate limit for a request.

        Parameters
        ----------
        path:
            Route path (template form, e.g. ``"/api/payments"``).
        method:
            HTTP method (``"GET"``, ``"POST"``, …).
        request:
            The live Starlette ``Request`` object.  When ``None`` (e.g. in
            tests that don't provide a request), the check is bypassed and
            an allowed result is returned.
        policy:
            The ``RateLimitPolicy`` registered for this route.
        custom_key_func:
            Callable ``(Request) -> str | None`` for ``CUSTOM`` strategy.
        """
        if request is None:
            return _allowed_result(policy.limit)

        # 1. Exemption check.
        if await is_exempt(request, policy):
            return _allowed_result(policy.limit)

        # 2. Resolve effective limit (tier-aware).
        effective_limit, tier = self._resolve_limit(policy, request)

        # 3. "unlimited" tier → skip storage entirely.
        if effective_limit.lower() == "unlimited":
            return _allowed_result("unlimited", tier=tier)

        # 4. Extract the key.
        extractor = self._get_extractor(policy, custom_func=custom_key_func)
        _extracted = extractor(request)
        raw_key: str | None = await _extracted if inspect.isawaitable(_extracted) else _extracted

        key_was_missing = False
        missing_behaviour: OnMissingKey | None = None

        # 5. Handle missing key.
        if raw_key is None:
            resolved_key, behaviour = await handle_missing_key(request, policy)
            key_was_missing = True
            missing_behaviour = behaviour

            if behaviour == OnMissingKey.EXEMPT:
                return RateLimitResult(
                    allowed=True,
                    limit=effective_limit,
                    remaining=_parse_limit_amount(effective_limit),
                    reset_at=datetime.now(UTC),
                    retry_after_seconds=0,
                    key="",
                    tier=tier,
                    key_was_missing=True,
                    missing_key_behaviour=OnMissingKey.EXEMPT,
                )

            if behaviour == OnMissingKey.BLOCK:
                return RateLimitResult(
                    allowed=False,
                    limit=effective_limit,
                    remaining=0,
                    reset_at=datetime.now(UTC),
                    retry_after_seconds=0,
                    key="",
                    tier=tier,
                    key_was_missing=True,
                    missing_key_behaviour=OnMissingKey.BLOCK,
                )

            # FALLBACK_IP — resolved_key is the IP string.
            raw_key = resolved_key

        # 6. Namespace the key.
        namespaced_key = f"{_KEY_PREFIX}:{method.upper()}:{path}:{raw_key}"

        # 7. Increment and return.
        rl_result = await self._storage.increment(
            key=namespaced_key,
            limit=effective_limit,
            algorithm=policy.algorithm,
        )
        # Attach tier and missing-key info from our higher-level logic.
        return RateLimitResult(
            allowed=rl_result.allowed,
            limit=rl_result.limit,
            remaining=rl_result.remaining,
            reset_at=rl_result.reset_at,
            retry_after_seconds=rl_result.retry_after_seconds,
            key=namespaced_key,
            tier=tier,
            key_was_missing=key_was_missing,
            missing_key_behaviour=missing_behaviour,
        )

    async def reset(self, path: str, method: str | None = None) -> None:
        """Reset rate limit counters for *path*.

        Called when a route transitions out of maintenance mode so clients
        aren't penalised for retrying during the window.

        Parameters
        ----------
        path:
            Route path (template form).
        method:
            When provided, only resets counters for this specific method.
            When ``None``, resets all methods via ``reset_all_for_path``.
        """
        if method:
            await self._storage.reset(f"{_KEY_PREFIX}:{method.upper()}:{path}")
        else:
            await self._storage.reset_all_for_path(path)

    async def startup(self) -> None:
        """Delegate startup to the storage layer."""
        await self._storage.startup()

    async def shutdown(self) -> None:
        """Delegate shutdown to the storage layer."""
        await self._storage.shutdown()


def _allowed_result(limit_str: str, tier: str | None = None) -> RateLimitResult:
    """Return a fully-populated allowed result without touching storage."""
    amount = _parse_limit_amount(limit_str)
    return RateLimitResult(
        allowed=True,
        limit=limit_str,
        remaining=amount,
        reset_at=datetime.now(UTC),
        retry_after_seconds=0,
        key="",
        tier=tier,
    )


def _parse_limit_amount(limit_str: str) -> int:
    """Extract the request count from a limit string like ``"100/minute"``."""
    if limit_str.lower() == "unlimited":
        return 9999999
    try:
        part = limit_str.split("/")[0].strip()
        return int(part)
    except (ValueError, IndexError):
        return 0
