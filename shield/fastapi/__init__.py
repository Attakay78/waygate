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

from shield.admin.app import ShieldAdmin
from shield.fastapi.decorators import (
    ResponseFactory,
    deprecated,
    disabled,
    env_only,
    force_active,
    maintenance,
    rate_limit,
)
from shield.fastapi.dependencies import ShieldGuard, configure_shield
from shield.fastapi.middleware import ShieldMiddleware
from shield.fastapi.openapi import apply_shield_to_openapi, setup_shield_docs
from shield.fastapi.router import ShieldRouter, scan_routes

__all__ = [
    "ShieldAdmin",
    "ShieldMiddleware",
    "ShieldRouter",
    "ShieldGuard",
    "configure_shield",
    "scan_routes",
    "apply_shield_to_openapi",
    "setup_shield_docs",
    "ResponseFactory",
    "maintenance",
    "env_only",
    "disabled",
    "deprecated",
    "force_active",
    "rate_limit",
]
