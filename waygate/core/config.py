"""Backend and engine factory — the single source of truth for configuration.

Both the CLI and application code import from here so that backend selection,
env-var names, and defaults are defined exactly once.

Configuration is loaded in priority order (highest wins):
  1. Explicit keyword arguments passed to ``make_backend()`` / ``make_engine()``
  2. Process environment variables (``os.environ``)
  3. ``.waygate`` file in the current working directory
  4. Built-in defaults

``.waygate`` file format (one ``KEY=value`` per line, ``#`` comments ignored)::

    WAYGATE_BACKEND=file
    WAYGATE_FILE_PATH=waygate-state.json
    WAYGATE_ENV=production

Environment variables
---------------------
WAYGATE_BACKEND      ``memory`` | ``file`` | ``redis`` | ``custom``
                    (default: ``memory``)
WAYGATE_FILE_PATH    Path to the state file — extension sets the format:
                    ``.json`` (default), ``.yaml`` / ``.yml``, ``.toml``
                    (default: ``waygate-state.json``)
WAYGATE_REDIS_URL    Redis connection URL
                    (default: ``redis://localhost:6379/0``)
WAYGATE_CUSTOM_PATH  Dotted import path to a zero-arg factory when
                    ``WAYGATE_BACKEND=custom``
                    (e.g. ``myapp.backends:make_backend``)
WAYGATE_ENV          Runtime environment name
                    (default: ``dev``)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from waygate.core.backends.base import WaygateBackend

if TYPE_CHECKING:
    from waygate.core.engine import WaygateEngine

# ---------------------------------------------------------------------------
# Public env-var constants — import these instead of hardcoding the names
# ---------------------------------------------------------------------------

ENV_BACKEND = "WAYGATE_BACKEND"
ENV_FILE_PATH = "WAYGATE_FILE_PATH"
ENV_REDIS_URL = "WAYGATE_REDIS_URL"
ENV_CUSTOM_PATH = "WAYGATE_CUSTOM_PATH"
ENV_CURRENT_ENV = "WAYGATE_ENV"

_DEFAULT_BACKEND = "memory"
_DEFAULT_FILE_PATH = "waygate-state.json"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_ENV = "dev"

# Name of the project-level config file that is auto-loaded.
_CONFIG_FILE = ".waygate"


# ---------------------------------------------------------------------------
# Config file loader
# ---------------------------------------------------------------------------


def _load_config_file(path: str | Path | None = None) -> dict[str, str]:
    """Parse a ``.waygate`` KEY=value file and return its contents as a dict.

    Lines starting with ``#`` and blank lines are ignored.
    Values are stripped of surrounding whitespace and optional quotes.

    Parameters
    ----------
    path:
        Explicit path to load.  When ``None`` the loader walks up from the
        current working directory looking for a ``.waygate`` file (stops at
        the filesystem root).  Returns an empty dict if no file is found.
    """
    candidates: list[Path] = []

    if path is not None:
        candidates = [Path(path)]
    else:
        # Walk up from cwd looking for .waygate
        current = Path.cwd()
        while True:
            candidates.append(current / _CONFIG_FILE)
            parent = current.parent
            if parent == current:
                break
            current = parent

    for candidate in candidates:
        if candidate.is_file():
            return _parse_dotenv(candidate)

    return {}


def _parse_dotenv(filepath: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines from *filepath* into a dict."""
    result: dict[str, str] = {}
    for raw_line in filepath.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = raw_value.strip().strip("\"'")
        if key:
            result[key] = value
    return result


def _getvar(key: str, file_cfg: dict[str, str], default: str) -> str:
    """Read *key* with priority: os.environ → .waygate file → default."""
    return os.environ.get(key) or file_cfg.get(key) or default


def _load_custom_backend(dotted_path: str) -> WaygateBackend:
    """Import and instantiate a custom backend from a dotted path.

    Parameters
    ----------
    dotted_path:
        ``"module.path:FactoryOrClass"`` — the part before ``:`` is the
        importable module; the part after is a callable that takes no
        required arguments and returns a ``WaygateBackend`` instance.

    Raises
    ------
    ValueError
        If the path is malformed, the module cannot be imported, the
        attribute does not exist, or the returned object is not a
        ``WaygateBackend``.
    """
    if ":" not in dotted_path:
        raise ValueError(
            f"WAYGATE_CUSTOM_PATH {dotted_path!r} is not a valid dotted path. "
            "Expected format: mypackage.module:FactoryOrClass"
        )

    module_path, _, attr = dotted_path.partition(":")
    try:
        import importlib
        import sys

        cwd = str(Path.cwd())
        if cwd not in sys.path:
            sys.path.insert(0, cwd)

        module = importlib.import_module(module_path)
        factory = getattr(module, attr)
        instance = factory()
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"Cannot load custom backend from {dotted_path!r}: {exc}") from exc

    if not isinstance(instance, WaygateBackend):
        raise TypeError(
            f"WAYGATE_CUSTOM_PATH {dotted_path!r} returned "
            f"{type(instance).__name__!r}, which does not extend WaygateBackend."
        )
    return instance


# ---------------------------------------------------------------------------
# Public factory functions
# ---------------------------------------------------------------------------


def make_backend(
    backend_type: str | None = None,
    file_path: str | None = None,
    redis_url: str | None = None,
    custom_path: str | None = None,
    config_file: str | None = None,
) -> WaygateBackend:
    """Construct a backend from explicit args, env vars, or the ``.waygate`` file.

    Priority: explicit arg > ``os.environ`` > ``.waygate`` file > default.

    Parameters
    ----------
    backend_type:
        ``"memory"``, ``"file"``, ``"redis"``, or ``"custom"``.
    file_path:
        Path for ``FileBackend``.
    redis_url:
        URL for ``RedisBackend``.
    custom_path:
        Dotted import path for a custom backend factory when
        ``backend_type="custom"``.  Falls back to ``WAYGATE_CUSTOM_PATH``.
    config_file:
        Path to a ``.waygate``-format config file.  ``None`` = auto-discover.
    """
    cfg = _load_config_file(config_file)

    btype = (backend_type or _getvar(ENV_BACKEND, cfg, _DEFAULT_BACKEND)).lower()

    if btype == "redis":
        from waygate.core.backends.redis import RedisBackend

        url = redis_url or _getvar(ENV_REDIS_URL, cfg, _DEFAULT_REDIS_URL)
        return RedisBackend(url=url)

    if btype == "file":
        from waygate.core.backends.file import FileBackend

        path = file_path or _getvar(ENV_FILE_PATH, cfg, _DEFAULT_FILE_PATH)
        return FileBackend(path=path)

    if btype == "memory":
        from waygate.core.backends.memory import MemoryBackend

        return MemoryBackend()

    if btype == "custom":
        dotted = custom_path or _getvar(ENV_CUSTOM_PATH, cfg, "")
        if not dotted:
            raise ValueError(
                "WAYGATE_BACKEND=custom requires WAYGATE_CUSTOM_PATH to be set.\n"
                "Example: WAYGATE_CUSTOM_PATH=myapp.backends:make_backend"
            )
        return _load_custom_backend(dotted)

    raise ValueError(
        f"Unknown WAYGATE_BACKEND value {btype!r}. Valid options: memory, file, redis, custom"
    )


def make_engine(
    backend_type: str | None = None,
    file_path: str | None = None,
    redis_url: str | None = None,
    custom_path: str | None = None,
    current_env: str | None = None,
    config_file: str | None = None,
) -> WaygateEngine:
    """Construct a fully configured ``WaygateEngine``.

    Priority for every setting: explicit arg > ``os.environ`` > ``.waygate``
    file > built-in default.

    Parameters
    ----------
    backend_type:
        ``"memory"``, ``"file"``, ``"redis"``, or ``"custom"``.
    file_path:
        Path for ``FileBackend``.
    redis_url:
        URL for ``RedisBackend``.
    custom_path:
        Dotted import path for a custom backend factory when
        ``backend_type="custom"``.  Falls back to ``WAYGATE_CUSTOM_PATH``.
    current_env:
        Runtime environment name (e.g. ``"production"``).
    config_file:
        Path to a ``.waygate``-format config file.  ``None`` = auto-discover.

    Returns
    -------
    WaygateEngine
    """
    from waygate.core.engine import WaygateEngine

    cfg = _load_config_file(config_file)

    backend = make_backend(
        backend_type=backend_type,
        file_path=file_path,
        redis_url=redis_url,
        custom_path=custom_path,
        config_file=config_file,
    )
    env = current_env or _getvar(ENV_CURRENT_ENV, cfg, _DEFAULT_ENV)
    return WaygateEngine(backend=backend, current_env=env)
