"""Webhook formatters for waygate state-change notifications.

Formatters are callables with the signature::

    (event: str, path: str, state: RouteState) -> dict[str, Any]

The returned dict is JSON-serialised and POSTed to the webhook URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from waygate.core.models import RouteState


def default_formatter(event: str, path: str, state: RouteState) -> dict[str, Any]:
    """Build a generic JSON payload for any HTTP webhook consumer.

    Parameters
    ----------
    event:
        The lifecycle event name (e.g. ``"enable"``, ``"maintenance_on"``,
        ``"disable"``).
    path:
        The route key that changed.
    state:
        Current ``RouteState`` after the change.

    Returns
    -------
    dict
        JSON-serialisable payload with ``event``, ``path``, ``reason``,
        ``timestamp``, and a full ``state`` dump.
    """
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
        "maintenance_on": "#FFA500",  # orange
        "maintenance_off": "#36A64F",  # green
        "disable": "#FF0000",  # red
        "enable": "#36A64F",  # green
        "env_gate": "#439FE0",  # blue
    }

    def __call__(self, event: str, path: str, state: RouteState) -> dict[str, Any]:
        """Format a state-change event as a Slack Incoming Webhook payload.

        Parameters
        ----------
        event:
            The lifecycle event name (e.g. ``"enable"``, ``"maintenance_on"``,
            ``"disable"``).
        path:
            The route key that changed.
        state:
            Current ``RouteState`` after the change.

        Returns
        -------
        dict
            Slack Incoming Webhook payload with a colour-coded attachment.
        """
        colour = self._COLOURS.get(event, "#888888")
        text = f"*{event.upper()}*: `{path}`"
        if state.reason:
            text += f"\n> {state.reason}"
        return {
            "attachments": [
                {
                    "color": colour,
                    "text": text,
                    "footer": "waygate",
                    "ts": int(datetime.now(UTC).timestamp()),
                }
            ]
        }
