"""WaygateFeatureClient — OpenFeature-backed flag evaluation API.

Phase 2 implementation. Stub present so the package imports cleanly.
"""

from __future__ import annotations

from typing import Any

from waygate.core.feature_flags._guard import _require_flags

_require_flags()


class _SyncWaygateFeatureClient:
    """Synchronous façade over :class:`WaygateFeatureClient`.

    Access via ``engine.sync.flag_client`` from sync route handlers.
    FastAPI runs ``def`` handlers in anyio worker threads, which is exactly
    the context this class is designed for.

    Because OpenFeature evaluation is CPU-bound (pure Python, no I/O), all
    methods call the underlying OpenFeature client directly — no thread
    bridge or event-loop interaction needed.

    Examples
    --------
    ::

        @router.get("/checkout")
        def checkout(request: Request):
            enabled = engine.sync.flag_client.get_boolean_value(
                "new_checkout", False, {"targeting_key": request.state.user_id}
            )
            if enabled:
                return checkout_v2()
            return checkout_v1()
    """

    __slots__ = ("_of_client",)

    def __init__(self, of_client: object) -> None:
        # ``of_client`` is the raw openfeature Client, not WaygateFeatureClient.
        self._of_client = of_client

    def get_boolean_value(
        self,
        flag_key: str,
        default: bool,
        ctx: object | None = None,
    ) -> bool:
        """Evaluate a boolean flag synchronously."""
        from waygate.core.feature_flags._context import to_of_context

        return self._of_client.get_boolean_value(flag_key, default, to_of_context(ctx))  # type: ignore[attr-defined, no-any-return, arg-type]

    def get_string_value(
        self,
        flag_key: str,
        default: str,
        ctx: object | None = None,
    ) -> str:
        """Evaluate a string flag synchronously."""
        from waygate.core.feature_flags._context import to_of_context

        return self._of_client.get_string_value(flag_key, default, to_of_context(ctx))  # type: ignore[attr-defined, no-any-return, arg-type]

    def get_integer_value(
        self,
        flag_key: str,
        default: int,
        ctx: object | None = None,
    ) -> int:
        """Evaluate an integer flag synchronously."""
        from waygate.core.feature_flags._context import to_of_context

        return self._of_client.get_integer_value(flag_key, default, to_of_context(ctx))  # type: ignore[attr-defined, no-any-return, arg-type]

    def get_float_value(
        self,
        flag_key: str,
        default: float,
        ctx: object | None = None,
    ) -> float:
        """Evaluate a float flag synchronously."""
        from waygate.core.feature_flags._context import to_of_context

        return self._of_client.get_float_value(flag_key, default, to_of_context(ctx))  # type: ignore[attr-defined, no-any-return, arg-type]

    def get_object_value(
        self,
        flag_key: str,
        default: dict,  # type: ignore[type-arg]
        ctx: object | None = None,
    ) -> dict:  # type: ignore[type-arg]
        """Evaluate a JSON/object flag synchronously."""
        from waygate.core.feature_flags._context import to_of_context

        return self._of_client.get_object_value(flag_key, default, to_of_context(ctx))  # type: ignore[attr-defined, no-any-return, arg-type]


class WaygateFeatureClient:
    """Thin wrapper around the OpenFeature client.

    Instantiated via ``engine.use_openfeature()``.
    Do not construct directly.
    """

    def __init__(self, domain: str = "waygate") -> None:
        from openfeature import api

        self._client = api.get_client(domain)
        self._domain = domain

    async def get_boolean_value(
        self,
        flag_key: str,
        default: bool,
        ctx: object | None = None,
    ) -> bool:
        from waygate.core.feature_flags._context import to_of_context

        return self._client.get_boolean_value(flag_key, default, to_of_context(ctx))  # type: ignore[arg-type]

    async def get_string_value(
        self,
        flag_key: str,
        default: str,
        ctx: object | None = None,
    ) -> str:
        from waygate.core.feature_flags._context import to_of_context

        return self._client.get_string_value(flag_key, default, to_of_context(ctx))  # type: ignore[arg-type]

    async def get_integer_value(
        self,
        flag_key: str,
        default: int,
        ctx: object | None = None,
    ) -> int:
        from waygate.core.feature_flags._context import to_of_context

        return self._client.get_integer_value(flag_key, default, to_of_context(ctx))  # type: ignore[arg-type]

    async def get_float_value(
        self,
        flag_key: str,
        default: float,
        ctx: object | None = None,
    ) -> float:
        from waygate.core.feature_flags._context import to_of_context

        return self._client.get_float_value(flag_key, default, to_of_context(ctx))  # type: ignore[arg-type]

    async def get_object_value(
        self,
        flag_key: str,
        default: dict[str, Any],
        ctx: object | None = None,
    ) -> dict[str, Any]:
        from waygate.core.feature_flags._context import to_of_context

        return self._client.get_object_value(flag_key, default, to_of_context(ctx))  # type: ignore[arg-type, return-value]

    @property
    def sync(self) -> _SyncWaygateFeatureClient:
        """Return a synchronous façade for use in ``def`` (non-async) handlers.

        Prefer ``engine.sync.flag_client`` over accessing this directly.
        """
        return _SyncWaygateFeatureClient(self._client)
