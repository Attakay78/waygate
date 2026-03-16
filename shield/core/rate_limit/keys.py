"""Request key extraction for rate limiting.

Key extractors are async callables ``(Request) -> str | None``.
Returning ``None`` signals a missing key — the caller applies
``on_missing_key`` behaviour.  **Extractors never raise.**

IP extraction follows slowapi's approach:
- ``X-Forwarded-For`` header (first IP in comma-separated list)
- ``X-Real-IP`` header
- ``request.client.host`` from the ASGI scope
- ``"unknown"`` fallback — the only extractor that is **never** ``None``

Missing-key handling
--------------------
``handle_missing_key`` is the single place where ``on_missing_key`` logic
is applied.  It always emits a ``WARNING`` log because a high rate of
missing-key events is a diagnostic signal that the key strategy is
misconfigured (typically: auth middleware not running before ShieldMiddleware).
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request

from shield.core.rate_limit.models import (
    OnMissingKey,
    RateLimitKeyStrategy,
    RateLimitPolicy,
    resolve_on_missing_key,
)

logger = logging.getLogger(__name__)

# Type alias for key extractor callables.
KeyExtractor = Callable[[Request], "Awaitable[str | None] | str | None"]


async def extract_ip(request: Request) -> str:
    """Extract the client IP address from the request.

    Checks headers in order:
    1. ``X-Forwarded-For`` — take the **first** IP in the comma-separated list
       (the original client, not a proxy).
    2. ``X-Real-IP``
    3. ``request.client.host``
    4. ``"unknown"`` — this extractor **never** returns ``None``.

    IPv4 port numbers are stripped.  IPv6 addresses are normalised via
    ``ipaddress.ip_address(ip).compressed``.
    """
    # 1. X-Forwarded-For (first IP is the original client)
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        ip = xff.split(",")[0].strip()
        return _normalise_ip(ip)

    # 2. X-Real-IP
    xri = request.headers.get("X-Real-IP")
    if xri:
        return _normalise_ip(xri.strip())

    # 3. ASGI scope client host
    if request.client and request.client.host:
        return _normalise_ip(request.client.host)

    return "unknown"


def _normalise_ip(ip: str) -> str:
    """Strip port from IPv4 and normalise IPv6 to compressed form."""
    # Strip IPv4 port (e.g. "192.168.1.1:12345" → "192.168.1.1")
    if ":" in ip and not ip.startswith("[") and ip.count(":") == 1:
        ip = ip.split(":")[0]
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except ValueError:
        return ip.strip()


async def extract_user(request: Request) -> str | None:
    """Return ``request.state.user_id`` or ``None`` if not set.

    Does **not** fall back to IP — that is the caller's responsibility via
    ``on_missing_key``.  Never raises.
    """
    return getattr(request.state, "user_id", None)


async def extract_api_key(request: Request) -> str | None:
    """Return the ``X-API-Key`` header value or ``None`` if absent.

    Does **not** fall back to IP — that is the caller's responsibility via
    ``on_missing_key``.  Never raises.
    """
    return request.headers.get("X-API-Key")


async def extract_global(request: Request) -> str:
    """Return the route path as the key.  Every caller shares one counter.

    Never missing.
    """
    return request.url.path


def resolve_key_extractor(
    strategy: RateLimitKeyStrategy,
    custom_func: KeyExtractor | None = None,
) -> KeyExtractor:
    """Return the appropriate extractor for *strategy*.

    Parameters
    ----------
    strategy:
        The key strategy enum value.
    custom_func:
        Required when *strategy* is ``CUSTOM``.  Raises ``ValueError`` if
        ``CUSTOM`` is selected without a callable.

    Returns
    -------
    KeyExtractor
        An async callable ``(Request) -> str | None``.
    """
    if strategy == RateLimitKeyStrategy.IP:
        return extract_ip
    if strategy == RateLimitKeyStrategy.USER:
        return extract_user
    if strategy == RateLimitKeyStrategy.API_KEY:
        return extract_api_key
    if strategy == RateLimitKeyStrategy.GLOBAL:
        return extract_global
    if strategy == RateLimitKeyStrategy.CUSTOM:
        if custom_func is None:
            raise ValueError(
                "key_strategy=CUSTOM requires a custom_func callable. "
                "Pass it as the key= argument to @rate_limit."
            )
        return custom_func
    raise ValueError(f"Unknown key strategy: {strategy!r}")


async def handle_missing_key(
    request: Request,
    policy: RateLimitPolicy,
) -> tuple[str | None, OnMissingKey]:
    """Apply ``on_missing_key`` behaviour when a key extractor returns ``None``.

    **Always emits a WARNING log** — a missing key is always a diagnostic
    signal worth surfacing.  A high rate of these warnings in production
    means the configured key strategy does not match the auth setup.

    Returns
    -------
    (resolved_key, behaviour_applied):
        - ``(None, EXEMPT)``          — caller skips rate limiting entirely.
        - ``(ip_string, FALLBACK_IP)`` — caller uses the IP as the key.
        - ``(None, BLOCK)``            — caller returns 429 immediately.
    """
    behaviour = resolve_on_missing_key(policy)

    logger.warning(
        "api-shield rate_limit: key strategy '%s' returned no value "
        "for %s %s — applying on_missing_key='%s'. "
        "If this is unexpected, check that your auth middleware sets "
        "the expected attribute on request.state before the rate "
        "limiter runs.",
        policy.key_strategy,
        request.method,
        request.url.path,
        behaviour,
    )

    if behaviour == OnMissingKey.EXEMPT:
        return None, behaviour

    if behaviour == OnMissingKey.FALLBACK_IP:
        ip_key = await extract_ip(request)
        return ip_key, behaviour

    if behaviour == OnMissingKey.BLOCK:
        return None, behaviour

    return None, OnMissingKey.EXEMPT  # unreachable but satisfies type checker


async def is_exempt(
    request: Request,
    policy: RateLimitPolicy,
) -> bool:
    """Check whether the request is exempt from rate limiting.

    Exemption criteria (either is sufficient):
    1. Client IP is in ``policy.exempt_ips`` (CIDR notation supported).
    2. ``request.state.user_roles`` intersects ``policy.exempt_roles``.

    Exempt requests bypass the rate limit entirely — no counter is
    incremented and no ``X-RateLimit-*`` headers are injected.
    """
    if policy.exempt_ips:
        client_ip_str = await extract_ip(request)
        try:
            client_ip = ipaddress.ip_address(client_ip_str)
            for cidr in policy.exempt_ips:
                try:
                    network = ipaddress.ip_network(cidr, strict=False)
                    if client_ip in network:
                        return True
                except ValueError:
                    continue
        except ValueError:
            pass

    if policy.exempt_roles:
        user_roles: Any = getattr(request.state, "user_roles", None)
        if user_roles:
            try:
                roles_set = set(user_roles)
                if roles_set.intersection(policy.exempt_roles):
                    return True
            except TypeError:
                pass

    return False
