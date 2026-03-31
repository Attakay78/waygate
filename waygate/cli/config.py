"""CLI configuration — manages ``~/.waygate/config.json``.

Provides cross-platform helpers for reading and writing the Waygate CLI
configuration file that stores the server URL and authentication token.

File location
-------------
* **macOS / Linux**: ``~/.waygate/config.json``
* **Windows**: ``%USERPROFILE%\\AppData\\Local\\waygate\\config.json``

Config schema
-------------
.. code-block:: json

    {
        "server_url": "http://localhost:8000/waygate",
        "auth": {
            "token": "<signed-token>",
            "username": "admin",
            "expires_at": "2026-03-15T10:00:00+00:00"
        }
    }

Server URL resolution order
----------------------------
1. ``WAYGATE_SERVER_URL`` environment variable
2. ``WAYGATE_SERVER_URL`` key in a ``.waygate`` file (walks up from cwd)
3. ``server_url`` key in ``~/.waygate/config.json``
4. Built-in default: ``http://localhost:8000/waygate``

The ``.waygate`` file uses the same key name as the environment variable::

    # .waygate  (commit alongside your code)
    WAYGATE_SERVER_URL=http://localhost:8000/waygate
"""

from __future__ import annotations

import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_SERVER_URL = "http://localhost:8000/waygate"


def get_config_dir() -> Path:
    """Return the platform-appropriate Waygate config directory.

    The directory is created if it does not already exist.
    """
    system = platform.system()
    if system == "Windows":
        base = Path.home() / "AppData" / "Local" / "waygate"
    else:
        base = Path.home() / ".waygate"
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_config_path() -> Path:
    """Return the full path to ``config.json``."""
    return get_config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    """Load the config file from disk.

    Returns an empty dict when the file does not exist or cannot be parsed.
    """
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {}


def save_config(config: dict[str, Any]) -> None:
    """Write *config* to disk as formatted JSON."""
    path = get_config_path()
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# .waygate file helpers
# ---------------------------------------------------------------------------


def find_waygate_file(start: Path | None = None) -> Path | None:
    """Walk up the directory tree from *start* looking for a ``.waygate`` file.

    Returns the first ``.waygate`` file found, or ``None`` if none exists.
    *start* defaults to the current working directory.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / ".waygate"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None  # reached filesystem root
        current = parent


def _parse_waygate_file(path: Path) -> dict[str, str]:
    """Parse a ``.waygate`` file into a ``{KEY: value}`` dict.

    Each line should be ``KEY=value`` (lines starting with ``#`` are ignored).
    """
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Server URL helpers
# ---------------------------------------------------------------------------


def get_server_url() -> str:
    """Return the server URL using the resolution chain.

    Resolution order:

    1. ``WAYGATE_SERVER_URL`` environment variable
    2. ``WAYGATE_SERVER_URL`` key in a ``.waygate`` file (walked up from cwd)
    3. ``server_url`` key in ``~/.waygate/config.json``
    4. Built-in default: ``http://localhost:8000/waygate``
    """
    # 1. Environment variable
    env_url = os.environ.get("WAYGATE_SERVER_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    # 2. .waygate file in the project tree
    waygate_file = find_waygate_file()
    if waygate_file:
        pairs = _parse_waygate_file(waygate_file)
        file_url = pairs.get("WAYGATE_SERVER_URL", "").strip()
        if file_url:
            return file_url.rstrip("/")

    # 3. User-level config.json
    cfg_url: str = load_config().get("server_url", "")
    if cfg_url:
        return cfg_url.rstrip("/")

    # 4. Built-in default
    return _DEFAULT_SERVER_URL


def get_server_url_source() -> str:
    """Return a human-readable string describing where the URL came from."""
    if os.environ.get("WAYGATE_SERVER_URL", "").strip():
        return "env WAYGATE_SERVER_URL"
    waygate_file = find_waygate_file()
    if waygate_file:
        pairs = _parse_waygate_file(waygate_file)
        if pairs.get("WAYGATE_SERVER_URL", "").strip():
            return f".waygate ({waygate_file})"
    if load_config().get("server_url"):
        return str(get_config_path())
    return "default"


def set_server_url(url: str) -> None:
    """Save *url* as the admin server URL in ``~/.waygate/config.json``."""
    cfg = load_config()
    cfg["server_url"] = url.rstrip("/")
    save_config(cfg)


def require_server_url() -> str:
    """Return the resolved server URL (always succeeds)."""
    return get_server_url()


# ---------------------------------------------------------------------------
# Auth token helpers
# ---------------------------------------------------------------------------


def get_auth_info() -> dict[str, Any]:
    """Return the ``auth`` section of the config, or an empty dict."""
    info: dict[str, Any] = load_config().get("auth", {})
    return info


def get_auth_token() -> str | None:
    """Return the stored auth token if it is still valid, else ``None``."""
    info = get_auth_info()
    if not info:
        return None
    expires_at = info.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            # Attach UTC if the stored string has no timezone.
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= datetime.now(UTC):
                return None  # token expired
        except Exception:
            pass
    return info.get("token")


def get_auth_username() -> str | None:
    """Return the stored username, or ``None``."""
    return get_auth_info().get("username")


def get_token_expires_at() -> str | None:
    """Return the ISO-8601 expiry string of the stored token, or ``None``."""
    return get_auth_info().get("expires_at")


def set_auth(token: str, username: str, expires_at: str) -> None:
    """Persist an auth token to the config file."""
    cfg = load_config()
    cfg["auth"] = {
        "token": token,
        "username": username,
        "expires_at": expires_at,
    }
    save_config(cfg)


def clear_auth() -> None:
    """Remove stored auth credentials from the config file."""
    cfg = load_config()
    cfg.pop("auth", None)
    save_config(cfg)


def is_authenticated() -> bool:
    """Return ``True`` when a valid (non-expired) token is stored."""
    return get_auth_token() is not None
