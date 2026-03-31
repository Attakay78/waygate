"""Waygate Admin — unified admin interface for waygate.

Exposes both the HTMX dashboard UI *and* a REST API that the ``waygate`` CLI
uses as its HTTP back-end.  Mount a single :func:`WaygateAdmin` instance on
your FastAPI / Starlette application and both interfaces are available
immediately.
"""

from waygate.admin.app import WaygateAdmin

__all__ = ["WaygateAdmin"]
