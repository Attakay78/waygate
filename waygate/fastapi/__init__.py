"""FastAPI adapter for waygate.

Exports the middleware, router, OpenAPI helper, and all decorators so that
users need only a single import line::

    from waygate.fastapi import (
        WaygateMiddleware,
        WaygateRouter,
        apply_waygate_to_openapi,
        maintenance,
        env_only,
        disabled,
        force_active,
    )
"""

from waygate.admin.app import WaygateAdmin
from waygate.admin.auth import WaygateAuthBackend, make_auth_backend
from waygate.fastapi.decorators import (
    ResponseFactory,
    deprecated,
    disabled,
    env_only,
    force_active,
    maintenance,
    rate_limit,
)
from waygate.fastapi.dependencies import WaygateGuard, configure_waygate
from waygate.fastapi.middleware import WaygateMiddleware
from waygate.fastapi.openapi import apply_waygate_to_openapi, setup_waygate_docs
from waygate.fastapi.router import WaygateRouter, scan_routes

__all__ = [
    "WaygateAdmin",
    "WaygateAuthBackend",
    "make_auth_backend",
    "WaygateMiddleware",
    "WaygateRouter",
    "WaygateGuard",
    "configure_waygate",
    "scan_routes",
    "apply_waygate_to_openapi",
    "setup_waygate_docs",
    "ResponseFactory",
    "maintenance",
    "env_only",
    "disabled",
    "deprecated",
    "force_active",
    "rate_limit",
]
