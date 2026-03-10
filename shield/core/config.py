"""Backend and engine factory — the single source of truth for configuration.

Both the CLI and application code import from here so that backend selection,
env-var names, and defaults are defined exactly once.

Configuration is loaded in priority order (highest wins):
  1. Explicit keyword arguments passed to ``make_backend()`` / ``make_engine()``
  2. Process environment variables (``os.environ``)
  3. ``.shield`` file in the current working directory
  4. Built-in defaults

``.shield`` file format (one ``KEY=value`` per line, ``#`` comments ignored)::

    SHIELD_BACKEND=file
    SHIELD_FILE_PATH=shield-state.json
    SHIELD_ENV=production

Environment variables
---------------------
SHIELD_BACKEND      ``memory`` | ``file`` | ``redis`` | ``custom``
                    (default: ``memory``)
SHIELD_FILE_PATH    Path to the state file — extension sets the format:
                    ``.json`` (default), ``.yaml`` / ``.yml``, ``.toml``
                    (default: ``shield-state.json``)
SHIELD_REDIS_URL    Redis connection URL
                    (default: ``redis://localhost:6379/0``)
SHIELD_CUSTOM_PATH  Dotted import path to a zero-arg factory when
                    ``SHIELD_BACKEND=custom``
                    (e.g. ``myapp.backends:make_backend``)
SHIELD_ENV          Runtime environment name
                    (default: ``production``)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from shield.core.backends.base import ShieldBackend

if TYPE_CHECKING:
    from shield.core.engine import ShieldEngine

# ---------------------------------------------------------------------------
# Public env-var constants — import these instead of hardcoding the names
# ---------------------------------------------------------------------------

ENV_BACKEND = "SHIELD_BACKEND"
ENV_FILE_PATH = "SHIELD_FILE_PATH"
ENV_REDIS_URL = "SHIELD_REDIS_URL"
ENV_CUSTOM_PATH = "SHIELD_CUSTOM_PATH"
ENV_CURRENT_ENV = "SHIELD_ENV"

_DEFAULT_BACKEND = "memory"
_DEFAULT_FILE_PATH = "shield-state.json"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_ENV = "production"

# Name of the project-level config file that is auto-loaded.
_CONFIG_FILE = ".shield"


# ---------------------------------------------------------------------------
# Config file loader
# ---------------------------------------------------------------------------


def _load_config_file(path: str | Path | None = None) -> dict[str, str]:
    """Parse a ``.shield`` KEY=value file and return its contents as a dict.

    Lines starting with ``#`` and blank lines are ignored.
    Values are stripped of surrounding whitespace and optional quotes.

    Parameters
    ----------
    path:
        Explicit path to load.  When ``None`` the loader walks up from the
        current working directory looking for a ``.shield`` file (stops at
        the filesystem root).  Returns an empty dict if no file is found.
    """
    candidates: list[Path] = []

    if path is not None:
        candidates = [Path(path)]
    else:
        # Walk up from cwd looking for .shield
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
    """Read *key* with priority: os.environ → .shield file → default."""
    return os.environ.get(key) or file_cfg.get(key) or default


def _load_custom_backend(dotted_path: str) -> ShieldBackend:
    """Import and instantiate a custom backend from a dotted path.

    Parameters
    ----------
    dotted_path:
        ``"module.path:FactoryOrClass"`` — the part before ``:`` is the
        importable module; the part after is a callable that takes no
        required arguments and returns a ``ShieldBackend`` instance.

    Raises
    ------
    ValueError
        If the path is malformed, the module cannot be imported, the
        attribute does not exist, or the returned object is not a
        ``ShieldBackend``.
    """
    if ":" not in dotted_path:
        raise ValueError(
            f"SHIELD_CUSTOM_PATH {dotted_path!r} is not a valid dotted path. "
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

    if not isinstance(instance, ShieldBackend):
        raise TypeError(
            f"SHIELD_CUSTOM_PATH {dotted_path!r} returned "
            f"{type(instance).__name__!r}, which does not extend ShieldBackend."
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
) -> ShieldBackend:
    """Construct a backend from explicit args, env vars, or the ``.shield`` file.

    Priority: explicit arg > ``os.environ`` > ``.shield`` file > default.

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
        ``backend_type="custom"``.  Falls back to ``SHIELD_CUSTOM_PATH``.
    config_file:
        Path to a ``.shield``-format config file.  ``None`` = auto-discover.
    """
    cfg = _load_config_file(config_file)

    btype = (backend_type or _getvar(ENV_BACKEND, cfg, _DEFAULT_BACKEND)).lower()

    if btype == "redis":
        from shield.core.backends.redis import RedisBackend

        url = redis_url or _getvar(ENV_REDIS_URL, cfg, _DEFAULT_REDIS_URL)
        return RedisBackend(url=url)

    if btype == "file":
        from shield.core.backends.file import FileBackend

        path = file_path or _getvar(ENV_FILE_PATH, cfg, _DEFAULT_FILE_PATH)
        return FileBackend(path=path)

    if btype == "memory":
        from shield.core.backends.memory import MemoryBackend

        return MemoryBackend()

    if btype == "custom":
        dotted = custom_path or _getvar(ENV_CUSTOM_PATH, cfg, "")
        if not dotted:
            raise ValueError(
                "SHIELD_BACKEND=custom requires SHIELD_CUSTOM_PATH to be set.\n"
                "Example: SHIELD_CUSTOM_PATH=myapp.backends:make_backend"
            )
        return _load_custom_backend(dotted)

    raise ValueError(
        f"Unknown SHIELD_BACKEND value {btype!r}. Valid options: memory, file, redis, custom"
    )


def make_engine(
    backend_type: str | None = None,
    file_path: str | None = None,
    redis_url: str | None = None,
    custom_path: str | None = None,
    current_env: str | None = None,
    config_file: str | None = None,
) -> ShieldEngine:
    """Construct a fully configured ``ShieldEngine``.

    Priority for every setting: explicit arg > ``os.environ`` > ``.shield``
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
        ``backend_type="custom"``.  Falls back to ``SHIELD_CUSTOM_PATH``.
    current_env:
        Runtime environment name (e.g. ``"production"``).
    config_file:
        Path to a ``.shield``-format config file.  ``None`` = auto-discover.

    Returns
    -------
    ShieldEngine
    """
    from shield.core.engine import ShieldEngine

    cfg = _load_config_file(config_file)

    backend = make_backend(
        backend_type=backend_type,
        file_path=file_path,
        redis_url=redis_url,
        custom_path=custom_path,
        config_file=config_file,
    )
    env = current_env or _getvar(ENV_CURRENT_ENV, cfg, _DEFAULT_ENV)
    return ShieldEngine(backend=backend, current_env=env)
