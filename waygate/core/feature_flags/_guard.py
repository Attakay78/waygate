"""Import guard for the feature flags optional dependency.

Call ``_require_flags()`` at the top of any module that needs
``openfeature`` before attempting to import it.  This produces a clear,
actionable error message instead of a bare ``ModuleNotFoundError``.

``waygate/core/feature_flags/models.py`` and ``evaluator.py`` are pure
Pydantic/stdlib and do **not** call this guard — they are importable
regardless of whether the [flags] extra is installed.  Only the public
``waygate.core.feature_flags`` namespace (``__init__.py``) and the
provider/client modules call this guard.
"""

from __future__ import annotations


def _require_flags() -> None:
    """Raise ``ImportError`` with install instructions if openfeature is missing."""
    try:
        import openfeature  # noqa: F401
    except ImportError:
        raise ImportError(
            "Feature flags require the [flags] extra.\n"
            "Install with:  pip install waygate[flags]\n"
            "Or:            uv pip install 'waygate[flags]'"
        ) from None
