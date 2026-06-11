"""Testing utilities for waygate.

Provides helpers for disabling waygate checks during tests without
requiring mocks or monkey-patching the engine internals.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from waygate.core.engine import WaygateEngine


@contextmanager
def bypass(
    engine: WaygateEngine,
    *,
    rate_limits: bool = True,
    lifecycle: bool = True,
) -> Generator[None, None, None]:
    """Temporarily disable waygate checks on *engine*.

    Restores the original flags when the block exits, even on exception.

    Parameters
    ----------
    engine:
        The ``WaygateEngine`` instance to modify.
    rate_limits:
        Disable rate limit checks while inside the block.
    lifecycle:
        Disable maintenance, disabled, and env-gated checks while inside
        the block. Routes behave as if they are active.

    Examples
    --------
    Bypass everything::

        from waygate.testing import bypass

        with bypass(engine):
            response = client.get("/limited-route")

    Bypass only rate limits, keep lifecycle checks::

        with bypass(engine, lifecycle=False):
            response = client.get("/limited-route")
    """
    prev_rl = engine.bypass_rate_limits
    prev_lc = engine.bypass_lifecycle
    engine.bypass_rate_limits = rate_limits
    engine.bypass_lifecycle = lifecycle
    try:
        yield
    finally:
        engine.bypass_rate_limits = prev_rl
        engine.bypass_lifecycle = prev_lc
