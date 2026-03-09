"""Tests for webhook support in ShieldEngine and formatters."""

from __future__ import annotations

import asyncio

from shield.core.backends.memory import MemoryBackend
from shield.core.engine import ShieldEngine
from shield.core.models import RouteStatus
from shield.core.webhooks import SlackWebhookFormatter, default_formatter

# ---------------------------------------------------------------------------
# Formatter unit tests
# ---------------------------------------------------------------------------


def _make_state():
    from shield.core.models import RouteState

    return RouteState(path="/api/pay", status=RouteStatus.MAINTENANCE, reason="DB mig")


def test_default_formatter_keys():
    state = _make_state()
    payload = default_formatter("maintenance_on", "/api/pay", state)
    assert payload["event"] == "maintenance_on"
    assert payload["path"] == "/api/pay"
    assert payload["reason"] == "DB mig"
    assert "timestamp" in payload
    assert "state" in payload


def test_slack_formatter_structure():
    state = _make_state()
    formatter = SlackWebhookFormatter()
    payload = formatter("maintenance_on", "/api/pay", state)
    assert "attachments" in payload
    attachments = payload["attachments"]
    assert len(attachments) == 1
    assert "color" in attachments[0]
    assert "/api/pay" in attachments[0]["text"]


def test_slack_formatter_colour_by_event():
    state = _make_state()
    formatter = SlackWebhookFormatter()
    orange = formatter("maintenance_on", "/api/pay", state)
    green = formatter("enable", "/api/pay", state)
    assert orange["attachments"][0]["color"] != green["attachments"][0]["color"]


# ---------------------------------------------------------------------------
# add_webhook() and fire-and-forget behaviour
# ---------------------------------------------------------------------------


async def test_add_webhook_registered():
    engine = ShieldEngine(backend=MemoryBackend())
    engine.add_webhook("http://example.com/hook")
    assert len(engine._webhooks) == 1


async def test_add_webhook_custom_formatter():
    engine = ShieldEngine(backend=MemoryBackend())
    fmt = SlackWebhookFormatter()
    engine.add_webhook("http://example.com/hook", formatter=fmt)
    _, registered_fmt = engine._webhooks[0]
    assert registered_fmt is fmt


async def test_webhook_fires_on_disable(monkeypatch):
    """Disabling a route triggers _fire_webhooks → asyncio.create_task."""
    fired: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict) -> None:
        fired.append((url, payload))

    engine = ShieldEngine(backend=MemoryBackend())
    monkeypatch.setattr(engine, "_post_webhook", staticmethod(fake_post))
    engine.add_webhook("http://hook.example/test")

    await engine.disable("/api/pay", reason="gone")
    # Let the background task run.
    await asyncio.sleep(0.05)

    assert len(fired) == 1
    url, payload = fired[0]
    assert url == "http://hook.example/test"
    assert payload["event"] == "disable"
    assert payload["path"] == "/api/pay"


async def test_webhook_fires_on_enable(monkeypatch):
    fired: list[dict] = []

    async def fake_post(url: str, payload: dict) -> None:
        fired.append(payload)

    engine = ShieldEngine(backend=MemoryBackend())
    monkeypatch.setattr(engine, "_post_webhook", staticmethod(fake_post))
    engine.add_webhook("http://hook.example/test")

    await engine.disable("/api/pay")
    await engine.enable("/api/pay")
    await asyncio.sleep(0.05)

    events = [p["event"] for p in fired]
    assert "enable" in events


async def test_webhook_fires_on_maintenance(monkeypatch):
    fired: list[dict] = []

    async def fake_post(url: str, payload: dict) -> None:
        fired.append(payload)

    engine = ShieldEngine(backend=MemoryBackend())
    monkeypatch.setattr(engine, "_post_webhook", staticmethod(fake_post))
    engine.add_webhook("http://hook.example/test")

    await engine.set_maintenance("/api/pay", reason="DB mig")
    await asyncio.sleep(0.05)

    assert any(p["event"] == "maintenance_on" for p in fired)


async def test_webhook_failure_does_not_affect_state(monkeypatch):
    """A crashing webhook must not raise or affect the returned state."""

    async def failing_post(url: str, payload: dict) -> None:
        raise RuntimeError("webhook server is down")

    engine = ShieldEngine(backend=MemoryBackend())
    monkeypatch.setattr(engine, "_post_webhook", staticmethod(failing_post))
    engine.add_webhook("http://broken.example/hook")

    # Must not raise.
    state = await engine.disable("/api/pay", reason="gone")
    await asyncio.sleep(0.1)

    # State is still correctly updated.
    assert state.status == RouteStatus.DISABLED


async def test_multiple_webhooks_all_called(monkeypatch):
    fired_urls: list[str] = []

    async def fake_post(url: str, payload: dict) -> None:
        fired_urls.append(url)

    engine = ShieldEngine(backend=MemoryBackend())
    monkeypatch.setattr(engine, "_post_webhook", staticmethod(fake_post))
    engine.add_webhook("http://hook1.example/")
    engine.add_webhook("http://hook2.example/")

    await engine.disable("/api/pay")
    await asyncio.sleep(0.05)

    assert "http://hook1.example/" in fired_urls
    assert "http://hook2.example/" in fired_urls
