"""Tests for waygate.core.rate_limit.models."""

from __future__ import annotations

from waygate.core.rate_limit.models import (
    STRATEGY_DEFAULTS,
    OnMissingKey,
    RateLimitAlgorithm,
    RateLimitHit,
    RateLimitKeyStrategy,
    RateLimitPolicy,
    RateLimitResult,
    RateLimitTier,
    resolve_on_missing_key,
)


class TestRateLimitAlgorithm:
    def test_values(self):
        assert RateLimitAlgorithm.FIXED_WINDOW == "fixed_window"
        assert RateLimitAlgorithm.SLIDING_WINDOW == "sliding_window"
        assert RateLimitAlgorithm.MOVING_WINDOW == "moving_window"
        assert RateLimitAlgorithm.TOKEN_BUCKET == "token_bucket"


class TestOnMissingKey:
    def test_values(self):
        assert OnMissingKey.EXEMPT == "exempt"
        assert OnMissingKey.FALLBACK_IP == "fallback_ip"
        assert OnMissingKey.BLOCK == "block"


class TestRateLimitKeyStrategy:
    def test_values(self):
        assert RateLimitKeyStrategy.IP == "ip"
        assert RateLimitKeyStrategy.USER == "user"
        assert RateLimitKeyStrategy.API_KEY == "api_key"
        assert RateLimitKeyStrategy.GLOBAL == "global"
        assert RateLimitKeyStrategy.CUSTOM == "custom"


class TestStrategyDefaults:
    def test_ip_not_in_defaults(self):
        # IP and GLOBAL are absent — they never produce a missing key.
        assert RateLimitKeyStrategy.IP not in STRATEGY_DEFAULTS

    def test_global_not_in_defaults(self):
        assert RateLimitKeyStrategy.GLOBAL not in STRATEGY_DEFAULTS

    def test_user_defaults_to_exempt(self):
        # Unauthenticated requests shouldn't be mixed into the authenticated counter
        assert STRATEGY_DEFAULTS[RateLimitKeyStrategy.USER] == OnMissingKey.EXEMPT

    def test_api_key_defaults_to_fallback_ip(self):
        # API_KEY missing → fall back to IP-based limiting
        assert STRATEGY_DEFAULTS[RateLimitKeyStrategy.API_KEY] == OnMissingKey.FALLBACK_IP

    def test_custom_has_default(self):
        assert RateLimitKeyStrategy.CUSTOM in STRATEGY_DEFAULTS


class TestResolveOnMissingKey:
    def test_explicit_on_missing_key_wins(self):
        policy = RateLimitPolicy(
            path="/test",
            method="GET",
            limit="10/minute",
            key_strategy=RateLimitKeyStrategy.USER,
            on_missing_key=OnMissingKey.BLOCK,
        )
        assert resolve_on_missing_key(policy) == OnMissingKey.BLOCK

    def test_falls_back_to_strategy_default(self):
        policy = RateLimitPolicy(
            path="/test",
            method="GET",
            limit="10/minute",
            key_strategy=RateLimitKeyStrategy.USER,
        )
        # USER default is EXEMPT
        assert resolve_on_missing_key(policy) == OnMissingKey.EXEMPT

    def test_api_key_default_is_fallback_ip(self):
        policy = RateLimitPolicy(
            path="/test",
            method="GET",
            limit="10/minute",
            key_strategy=RateLimitKeyStrategy.API_KEY,
        )
        assert resolve_on_missing_key(policy) == OnMissingKey.FALLBACK_IP


class TestRateLimitPolicy:
    def test_defaults(self):
        policy = RateLimitPolicy(path="/api/test", method="GET", limit="100/minute")
        assert policy.algorithm == RateLimitAlgorithm.FIXED_WINDOW
        assert policy.key_strategy == RateLimitKeyStrategy.IP
        assert policy.on_missing_key is None
        assert policy.burst == 0
        assert policy.tiers == []
        assert policy.exempt_ips == []
        assert policy.exempt_roles == []

    def test_model_dump(self):
        policy = RateLimitPolicy(
            path="/api/test",
            method="GET",
            limit="100/minute",
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        d = policy.model_dump(mode="json")
        assert d["path"] == "/api/test"
        assert d["limit"] == "100/minute"
        assert d["algorithm"] == "fixed_window"


class TestRateLimitTier:
    def test_basic(self):
        tier = RateLimitTier(name="premium", limit="1000/minute")
        assert tier.name == "premium"
        assert tier.limit == "1000/minute"

    def test_unlimited(self):
        tier = RateLimitTier(name="enterprise", limit="unlimited")
        assert tier.limit == "unlimited"


class TestRateLimitResult:
    def test_allowed_result(self):
        from datetime import UTC, datetime

        result = RateLimitResult(
            allowed=True,
            limit="100/minute",
            remaining=99,
            reset_at=datetime.now(UTC),
            retry_after_seconds=0,
            key="127.0.0.1",
        )
        assert result.allowed is True
        assert result.remaining == 99

    def test_blocked_result(self):
        from datetime import UTC, datetime

        result = RateLimitResult(
            allowed=False,
            limit="10/second",
            remaining=0,
            reset_at=datetime.now(UTC),
            key="10.0.0.1",
            retry_after_seconds=1,
        )
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after_seconds == 1


class TestRateLimitHit:
    def test_basic(self):
        import uuid
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        hit = RateLimitHit(
            id=str(uuid.uuid4()),
            path="/api/pay",
            method="POST",
            key="10.0.0.1",
            limit="10/minute",
            timestamp=now,
            reset_at=now,
        )
        assert hit.path == "/api/pay"
        assert hit.method == "POST"
        d = hit.model_dump(mode="json")
        assert d["path"] == "/api/pay"
