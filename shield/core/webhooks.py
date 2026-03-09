"""Webhook formatters for api-shield state-change notifications.

Formatters are callables with the signature::

    (event: str, path: str, state: RouteState) -> dict[str, Any]

The returned dict is JSON-serialised and POSTed to the webhook URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shield.core.models import RouteState


def default_formatter(event: str, path: str, state: RouteState) -> dict[str, Any]:
    """Generic JSON payload suitable for any HTTP webhook consumer."""
    return {
        "event": event,
        "path": path,
        "reason": state.reason,
        "timestamp": datetime.now(UTC).isoformat(),
        "state": state.model_dump(mode="json"),
    }


class SlackWebhookFormatter:
    """Format state-change events as a Slack Incoming Webhook message block.

    Pass an instance of this class as the ``formatter`` argument to
    ``engine.add_webhook()``.
    """

    # Colours for the Slack attachment sidebar.
    _COLOURS = {
        "maintenance_on": "#FFA500",   # orange
        "maintenance_off": "#36A64F",  # green
        "disable": "#FF0000",          # red
        "enable": "#36A64F",           # green
        "env_gate": "#439FE0",         # blue
    }

    def __call__(
        self, event: str, path: str, state: RouteState
    ) -> dict[str, Any]:
        """Return a Slack Incoming Webhook payload."""
        colour = self._COLOURS.get(event, "#888888")
        text = f"*{event.upper()}*: `{path}`"
        if state.reason:
            text += f"\n> {state.reason}"
        return {
            "attachments": [
                {
                    "color": colour,
                    "text": text,
                    "footer": "api-shield",
                    "ts": int(datetime.now(UTC).timestamp()),
                }
            ]
        }
