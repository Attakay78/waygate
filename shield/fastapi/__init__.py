"""FastAPI adapter for api-shield.

Exports the middleware, router, OpenAPI helper, and all decorators so that
users need only a single import line::

    from shield.fastapi import (
        ShieldMiddleware,
        ShieldRouter,
        apply_shield_to_openapi,
        maintenance,
        env_only,
        disabled,
        force_active,
    )
"""

from shield.fastapi.decorators import (
    deprecated,
    disabled,
    env_only,
    force_active,
    maintenance,
)
from shield.fastapi.dependencies import ShieldGuard, configure_shield
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.openapi import apply_shield_to_openapi, setup_shield_docs
from shield.fastapi.router import ShieldRouter, scan_routes

__all__ = [
    "ShieldMiddleware",
    "ShieldRouter",
    "ShieldGuard",
    "configure_shield",
    "scan_routes",
    "apply_shield_to_openapi",
    "setup_shield_docs",
    "maintenance",
    "env_only",
    "disabled",
    "deprecated",
    "force_active",
]
