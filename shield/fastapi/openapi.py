"""OpenAPI schema filter and docs UI customisation for api-shield.

``apply_shield_to_openapi`` monkey-patches ``app.openapi()`` so that the
generated schema reflects the current route lifecycle state:

- ``DISABLED``    — hidden from ``/docs`` and ``/redoc``.
- ``ENV_GATED``   — hidden when the current env is not in ``allowed_envs``.
- ``DEPRECATED``  — marked ``deprecated: true``.
- ``MAINTENANCE`` — ``x-shield-status`` extension added; markdown warning
                    prepended to the operation description; summary prefixed
                    with ``🔧 ``.
- **Global maintenance ON** — ``x-shield-global-maintenance`` extension added
  to ``info``; every non-exempt operation annotated as maintenance; a global
  warning prepended to ``info.description`` (visible in ReDoc).

``setup_shield_docs`` replaces both ``/docs`` (Swagger UI) and ``/redoc``
with versions that inject:

* A full-width sticky red banner when global maintenance is active.
* A small green "All systems operational" chip when it is inactive.
* Per-operation orange badges for routes in maintenance mode.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from fastapi import FastAPI
from starlette.responses import HTMLResponse

from shield.core.engine import ShieldEngine
from shield.core.models import GlobalMaintenanceConfig, RouteState, RouteStatus

# ---------------------------------------------------------------------------
# Maintenance description banner (rendered in Swagger UI expanded view + ReDoc)
# ---------------------------------------------------------------------------

_MAINTENANCE_DESCRIPTION_BANNER = (
    "> 🔧 **Shield: MAINTENANCE** — {reason}\n>\n"
    "> This endpoint is temporarily unavailable.  "
    "Check back later or contact your API team.\n\n"
)


def apply_shield_to_openapi(app: FastAPI, engine: ShieldEngine) -> None:
    """Patch ``app.openapi()`` to filter paths based on shield route state.

    Call this once after constructing the app, before serving requests.

    Parameters
    ----------
    app:
        The FastAPI application whose OpenAPI schema will be patched.
    engine:
        The ``ShieldEngine`` that owns all route state.
    """
    original_openapi = app.openapi

    def patched_openapi() -> dict[str, Any]:
        base = original_openapi()
        states = _fetch_states(engine)
        global_cfg = _fetch_global_config(engine)
        if states is None:
            return base

        state_map = {s.path: s for s in states}

        # Shallow-copy the top-level schema so we never mutate
        # ``self.openapi_schema`` (FastAPI's cache).  Every call must
        # read fresh state from the engine and recompute the filtered
        # paths from scratch against the original unmodified path list.
        schema: dict[str, Any] = {**base}
        original_paths: dict[str, Any] = dict(base.get("paths", {}))
        filtered: dict[str, Any] = {}

        _http_verbs = (
            "get", "post", "put", "patch", "delete", "head", "options"
        )

        for path, path_item in original_paths.items():
            # Resolve state using the same priority as engine.check():
            #   1. Per-HTTP-method state  → "GET:/payments"
            #   2. Path-level state       → "/payments"  (all methods)
            path_state = state_map.get(path)

            # Collect per-operation states from the schema's HTTP verbs.
            # OpenAPI method keys are lowercase: "get", "post", etc.
            op_states: dict[str, RouteState | None] = {}
            for http_verb in _http_verbs:
                if http_verb not in path_item:
                    continue
                method_key = f"{http_verb.upper()}:{path}"
                op_states[http_verb] = state_map.get(method_key) or path_state

            # If no state at all — pass through unchanged.
            if path_state is None and not any(op_states.values()):
                filtered[path] = path_item
                continue

            # Build the filtered path item, processing each HTTP verb.
            patched_item: dict[str, Any] = {}
            any_op_visible = False

            for key, value in path_item.items():
                if key not in op_states:
                    # Non-operation keys (parameters, summary, etc.) — keep.
                    patched_item[key] = value
                    continue

                state = op_states[key]
                if state is None:
                    patched_item[key] = value
                    any_op_visible = True
                    continue

                if state.status == RouteStatus.DISABLED:
                    continue  # hide this operation

                if state.status == RouteStatus.ENV_GATED:
                    if engine.current_env not in state.allowed_envs:
                        continue  # hide in this env

                if state.status == RouteStatus.DEPRECATED and isinstance(
                    value, dict
                ):
                    patched_item[key] = {**value, "deprecated": True}

                elif (
                    state.status == RouteStatus.MAINTENANCE
                    and isinstance(value, dict)
                ):
                    patched_item[key] = _annotate_maintenance(value, state)

                else:
                    patched_item[key] = value

                any_op_visible = True

            # Only include the path if at least one operation survived.
            if any_op_visible:
                filtered[path] = patched_item

        # ----------------------------------------------------------------
        # Global maintenance overlay
        #
        # If global maintenance is active, stamp every surviving non-exempt
        # operation that isn't already marked as maintenance.
        # ----------------------------------------------------------------
        if global_cfg is not None and global_cfg.enabled:
            exempt = set(global_cfg.exempt_paths)
            global_state = RouteState(
                path="__global__",
                status=RouteStatus.MAINTENANCE,
                reason=global_cfg.reason,
            )
            for path in list(filtered.keys()):
                path_item = filtered[path]
                patched_for_global: dict[str, Any] = {}
                changed = False
                for key, value in path_item.items():
                    if key not in _http_verbs or not isinstance(value, dict):
                        patched_for_global[key] = value
                        continue
                    # Skip if already marked (per-route maintenance wins).
                    if value.get("x-shield-status") == "maintenance":
                        patched_for_global[key] = value
                        continue
                    method_key = f"{key.upper()}:{path}"
                    if path in exempt or method_key in exempt:
                        patched_for_global[key] = value
                        continue
                    patched_for_global[key] = _annotate_maintenance(
                        value, global_state
                    )
                    changed = True
                if changed:
                    filtered[path] = patched_for_global

            # Embed global maintenance metadata in info for the injected JS.
            # The sticky HTML banner (rendered by setup_shield_docs) is the
            # single visible indicator — no extra text is injected into
            # info.description to avoid duplicate banners.
            schema["info"] = dict(schema.get("info", {}))
            schema["info"]["x-shield-global-maintenance"] = {
                "enabled": True,
                "reason": global_cfg.reason or "",
                "exempt_paths": list(global_cfg.exempt_paths),
            }
        else:
            # Global maintenance is OFF — signal this explicitly so the JS
            # can show the "all clear" chip and remove any stale banner.
            schema["info"] = dict(schema.get("info", {}))
            schema["info"]["x-shield-global-maintenance"] = {"enabled": False}

        schema["paths"] = filtered
        return schema

    app.openapi = patched_openapi  # type: ignore[method-assign]


def _annotate_maintenance(
    operation: dict[str, Any], state: RouteState
) -> dict[str, Any]:
    """Return a copy of *operation* annotated with maintenance indicators.

    Changes applied:

    * ``x-shield-status`` / ``x-shield-reason`` — machine-readable extensions
      picked up by ``setup_shield_docs`` to apply visual styling in Swagger UI.
    * ``description`` — markdown warning block prepended so it is visible in
      both the Swagger UI expanded view and ReDoc.
    * ``summary`` — ``🔧 `` prefix added so the maintenance state is visible
      in the collapsed route list without needing to expand the operation.
    """
    patched = dict(operation)

    # Machine-readable extension fields.
    patched["x-shield-status"] = "maintenance"
    patched["x-shield-reason"] = state.reason or ""

    # Prepend a markdown warning block to the description.
    fallback = "Temporarily unavailable"
    banner = _MAINTENANCE_DESCRIPTION_BANNER.format(reason=state.reason or fallback)
    existing_desc = patched.get("description") or ""
    patched["description"] = banner + existing_desc

    # Prefix the summary so the status is visible in the collapsed list.
    summary = patched.get("summary") or ""
    if not summary.startswith("🔧"):
        patched["summary"] = f"🔧 {summary}" if summary else "🔧 Maintenance"

    return patched


# ---------------------------------------------------------------------------
# Custom Swagger UI / ReDoc with global maintenance awareness
# ---------------------------------------------------------------------------

# CSS + JS injected into both the Swagger UI and ReDoc pages.
#
# Responsibilities:
#  1. Read ``info['x-shield-global-maintenance']`` from the live OpenAPI spec.
#  2. Show a full-width pulsing red sticky banner when global maintenance is ON.
#  3. Show a small green "All systems operational" chip when it is OFF.
#  4. Add per-operation orange badges for routes with x-shield-status=maintenance.
#  5. Re-fetch the spec every 15 seconds so the UI reacts to live changes
#     (enable/disable via CLI or admin endpoint) without a page refresh.
_SHIELD_DOCS_SCRIPT = """\
<style>
  /* --- Global maintenance banner (shown when enabled) --- */
  #shield-global-banner {
    position: sticky;
    top: 0;
    z-index: 99999;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 18px 32px;
    background: linear-gradient(135deg, #c62828 0%, #e53935 100%);
    color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    text-align: center;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    animation: shield-pulse 2.5s ease-in-out infinite;
  }
  @keyframes shield-pulse {
    0%, 100% { box-shadow: 0 4px 16px rgba(198,40,40,0.5); }
    50%       { box-shadow: 0 4px 32px rgba(198,40,40,0.9); }
  }
  #shield-global-banner .shield-banner-title {
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  #shield-global-banner .shield-banner-reason {
    font-size: 15px;
    opacity: 0.92;
    margin-bottom: 4px;
  }
  #shield-global-banner .shield-banner-exempt {
    font-size: 12px;
    opacity: 0.75;
    font-style: italic;
  }

  /* --- "All systems operational" chip (shown when disabled) --- */
  #shield-ok-chip {
    position: fixed;
    bottom: 18px;
    right: 18px;
    z-index: 99999;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    background: #1b5e20;
    color: #fff;
    border-radius: 24px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px;
    font-weight: 600;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    opacity: 0.92;
    cursor: default;
    user-select: none;
  }
  #shield-ok-chip .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #69f0ae;
    animation: shield-blink 2s ease-in-out infinite;
  }
  @keyframes shield-blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }

  /* --- Per-operation maintenance styling --- */
  .shield-maintenance-block {
    border-left: 4px solid #ff9800 !important;
    background: rgba(255, 152, 0, 0.07) !important;
  }
  .shield-maintenance-badge {
    display: inline-block;
    background: #ff9800;
    color: #fff;
    border-radius: 3px;
    padding: 2px 7px;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 0.5px;
    margin-left: 8px;
    vertical-align: middle;
  }
</style>
<script>
(function () {
  var POLL_INTERVAL_MS = 120000;  /* re-fetch spec every 2 min (visible tabs only) */
  var openapiUrl = '/openapi.json';

  /* ------------------------------------------------------------------ */
  /* Global banner                                                         */
  /* ------------------------------------------------------------------ */

  function applyGlobalBanner(spec) {
    var gm = (spec.info || {})['x-shield-global-maintenance'] || {};
    var bannerEl = document.getElementById('shield-global-banner');
    var chipEl   = document.getElementById('shield-ok-chip');

    if (gm.enabled) {
      /* Remove "all clear" chip if present */
      if (chipEl) chipEl.remove();

      if (!bannerEl) {
        bannerEl = document.createElement('div');
        bannerEl.id = 'shield-global-banner';
        document.body.insertBefore(bannerEl, document.body.firstChild);
      }

      var reason  = gm.reason || 'System maintenance in progress';
      var exempts = (gm.exempt_paths || []);
      var exemptHtml = exempts.length
        ? '<div class="shield-banner-exempt">Exempt: ' +
          exempts.join(' &bull; ') + '</div>'
        : '';

      bannerEl.innerHTML =
        '<div class="shield-banner-title">🔴 Site-Wide Maintenance In Progress</div>' +
        '<div class="shield-banner-reason">' + escHtml(reason) + '</div>' +
        exemptHtml;

    } else {
      /* Remove banner if present */
      if (bannerEl) bannerEl.remove();

      if (!chipEl) {
        chipEl = document.createElement('div');
        chipEl.id = 'shield-ok-chip';
        chipEl.title = 'Global maintenance is OFF — all routes are serving normally';
        chipEl.innerHTML = '<span class="dot"></span> All systems operational';
        document.body.appendChild(chipEl);
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* Per-operation maintenance badges                                      */
  /* ------------------------------------------------------------------ */

  function buildMaintenanceMap(spec) {
    var map = {};
    var paths = spec.paths || {};
    Object.keys(paths).forEach(function (path) {
      var methods = paths[path] || {};
      ['get','post','put','patch','delete','head','options'].forEach(function (m) {
        var op = methods[m];
        if (op && op['x-shield-status'] === 'maintenance') {
          map[m.toUpperCase() + ':' + path] = op['x-shield-reason'] || '';
        }
      });
    });
    return map;
  }

  function applyBadges(maintenanceMap) {
    document.querySelectorAll('.opblock').forEach(function (el) {
      var methodEl = el.querySelector('.opblock-summary-method');
      var pathEl   = el.querySelector('.opblock-summary-path b') ||
                     el.querySelector('.opblock-summary-path');
      if (!methodEl || !pathEl) return;

      var method = (methodEl.textContent || '').trim().toUpperCase();
      var path   = (pathEl.textContent   || '').trim();
      var key    = method + ':' + path;

      if (!(key in maintenanceMap)) return;
      if (el.classList.contains('shield-maintenance-block')) return;

      el.classList.add('shield-maintenance-block');

      var summary = el.querySelector('.opblock-summary');
      if (summary && !summary.querySelector('.shield-maintenance-badge')) {
        var badge = document.createElement('span');
        badge.className   = 'shield-maintenance-badge';
        badge.textContent = '🔧 MAINTENANCE';
        badge.title       = maintenanceMap[key] || 'Route in maintenance mode';
        summary.appendChild(badge);
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Spec application                                                      */
  /* ------------------------------------------------------------------ */

  var _observer = null;
  var _lastMap  = {};

  function applySpec(spec) {
    applyGlobalBanner(spec);

    var map = buildMaintenanceMap(spec);
    _lastMap = map;
    if (Object.keys(map).length > 0) {
      applyBadges(map);
      if (!_observer) {
        _observer = new MutationObserver(function () { applyBadges(_lastMap); });
        _observer.observe(document.body, { childList: true, subtree: true });
      }
    } else if (_observer) {
      _observer.disconnect();
      _observer = null;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Visibility-aware fetch                                                */
  /*                                                                      */
  /* Polls only when the tab is visible so background tabs never hit the  */
  /* server.  Uses the Page Visibility API (supported in all browsers     */
  /* since 2013).  When the tab comes back into view the spec is fetched  */
  /* immediately so the banner reflects the current state right away.     */
  /* ------------------------------------------------------------------ */

  function fetchAndApply() {
    /* Never request while the tab is hidden — skip this cycle. */
    if (document.hidden) return;
    fetch(openapiUrl)
      .then(function (r) { return r.json(); })
      .then(applySpec)
      .catch(function () { /* silently ignore — never break the page */ });
  }

  /* Fetch immediately when the tab becomes visible again. */
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) fetchAndApply();
  });

  /* ------------------------------------------------------------------ */
  /* Helpers                                                               */
  /* ------------------------------------------------------------------ */

  function escHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  /* ------------------------------------------------------------------ */
  /* Bootstrap                                                             */
  /* ------------------------------------------------------------------ */

  var urlAttr = document.querySelector('[data-openapi-url]');
  if (urlAttr) openapiUrl = urlAttr.dataset.openapiUrl;

  function start() {
    /* Initial fetch — slight delay for SwaggerUI / ReDoc to mount. */
    setTimeout(function () {
      fetchAndApply();
      /* Poll at 2-minute intervals — only fires when the tab is visible. */
      setInterval(fetchAndApply, POLL_INTERVAL_MS);
    }, 300);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
</script>
"""


def setup_shield_docs(app: FastAPI, engine: ShieldEngine) -> None:
    """Replace the default ``/docs`` and ``/redoc`` with shield-aware versions.

    Both pages gain:

    * **Global maintenance ON** — a full-width pulsing red sticky banner at
      the very top of the page with the reason and exempt routes listed.
    * **Global maintenance OFF** — a small green "All systems operational"
      chip in the bottom-right corner so the current state is always visible.
    * **Per-route maintenance** — orange left-border + ``🔧 MAINTENANCE``
      badge on affected Swagger UI operation blocks.

    The spec is re-fetched every 15 seconds so the UI reflects live changes
    (CLI ``shield global enable/disable``) without a page reload.

    Call *after* ``apply_shield_to_openapi``::

        apply_shield_to_openapi(app, engine)
        setup_shield_docs(app, engine)

    Parameters
    ----------
    app:
        The FastAPI application instance.
    engine:
        Passed for API consistency; the live state is read from the spec
        at render time by the injected JavaScript, not at setup time.
    """
    docs_url: str = app.docs_url or "/docs"
    redoc_url: str = app.redoc_url or "/redoc"
    openapi_url: str = app.openapi_url or "/openapi.json"

    from starlette.routing import Route

    # Remove both built-in docs routes so ours are the only matches.
    app.routes[:] = [
        r for r in app.routes
        if not (
            isinstance(r, Route)
            and getattr(r, "path", None) in (docs_url, redoc_url)
        )
    ]

    def _inject(base_html: str) -> str:
        """Embed openapi URL + shield script into an HTML page."""
        base_html = base_html.replace(
            "<body>",
            f'<body data-openapi-url="{openapi_url}">',
            1,
        )
        return base_html.replace("</body>", f"{_SHIELD_DOCS_SCRIPT}\n</body>", 1)

    @app.get(docs_url, include_in_schema=False)
    async def shield_swagger_ui() -> HTMLResponse:
        from fastapi.openapi.docs import get_swagger_ui_html

        base: str = get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{app.title} - Swagger UI",
        ).body.decode()
        return HTMLResponse(_inject(base))

    @app.get(redoc_url, include_in_schema=False)
    async def shield_redoc() -> HTMLResponse:
        from fastapi.openapi.docs import get_redoc_html

        base: str = get_redoc_html(
            openapi_url=openapi_url,
            title=f"{app.title} - ReDoc",
        ).body.decode()
        return HTMLResponse(_inject(base))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_states(engine: ShieldEngine) -> list[RouteState] | None:
    """Fetch all route states from both sync and async call contexts."""
    return _run_async(engine.list_states())


def _fetch_global_config(engine: ShieldEngine) -> GlobalMaintenanceConfig | None:
    """Fetch the global maintenance config from both sync and async contexts."""
    return _run_async(engine.get_global_maintenance())


def _run_async(coro: object) -> Any:
    """Run *coro* whether we are inside a running event loop or not."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    try:
        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, coro)  # type: ignore[arg-type]
                return fut.result(timeout=5)
        elif loop is not None:
            return loop.run_until_complete(coro)  # type: ignore[arg-type]
        else:
            return asyncio.run(coro)  # type: ignore[arg-type]
    except Exception:
        return None
