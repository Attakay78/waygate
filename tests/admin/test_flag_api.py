"""Tests for the feature flag + segment REST API endpoints in WaygateAdmin.

All tests use an in-process ASGI transport — no real server needed.
The admin is mounted with ``enable_flags=True`` so flag routes are active.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from waygate.admin.app import WaygateAdmin
from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.models import (
    FeatureFlag,
    Segment,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> WaygateEngine:
    return WaygateEngine()


@pytest.fixture
def admin(engine: WaygateEngine):
    """WaygateAdmin with flags enabled, no auth."""
    return WaygateAdmin(engine=engine, enable_flags=True)


@pytest.fixture
async def client(admin) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=admin),
        base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bool_flag_payload(key: str = "my_flag", enabled: bool = True) -> dict:
    return {
        "key": key,
        "name": "My Flag",
        "type": "boolean",
        "variations": [
            {"name": "on", "value": True},
            {"name": "off", "value": False},
        ],
        "off_variation": "off",
        "fallthrough": "off",
        "enabled": enabled,
    }


def _segment_payload(key: str = "beta") -> dict:
    return {
        "key": key,
        "name": "Beta Users",
        "included": ["user_1", "user_2"],
        "excluded": [],
        "rules": [],
    }


# ---------------------------------------------------------------------------
# Flag API — enable_flags=False (routes not mounted)
# ---------------------------------------------------------------------------


class TestFlagsNotMounted:
    async def test_flag_routes_absent_when_disabled(self, engine):
        admin_no_flags = WaygateAdmin(engine=engine, enable_flags=False)
        async with AsyncClient(
            transport=ASGITransport(app=admin_no_flags),
            base_url="http://testserver",
        ) as c:
            resp = await c.get("/api/flags")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Flag API — list
# ---------------------------------------------------------------------------


class TestListFlags:
    async def test_empty(self, client):
        resp = await client.get("/api/flags")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_saved_flag(self, client, engine):

        flag = FeatureFlag.model_validate(_bool_flag_payload())
        await engine.save_flag(flag)

        resp = await client.get("/api/flags")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "my_flag"


# ---------------------------------------------------------------------------
# Flag API — create
# ---------------------------------------------------------------------------


class TestCreateFlag:
    async def test_create_returns_201(self, client):
        resp = await client.post("/api/flags", json=_bool_flag_payload())
        assert resp.status_code == 201
        assert resp.json()["key"] == "my_flag"

    async def test_create_persists_flag(self, client, engine):
        await client.post("/api/flags", json=_bool_flag_payload())
        flag = await engine.get_flag("my_flag")
        assert flag is not None
        assert flag.key == "my_flag"

    async def test_create_conflict_returns_409(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.post("/api/flags", json=_bool_flag_payload())
        assert resp.status_code == 409

    async def test_create_invalid_body_returns_400(self, client):
        resp = await client.post("/api/flags", json={"key": "bad"})
        assert resp.status_code == 400

    async def test_create_string_flag(self, client):
        payload = {
            "key": "color_flag",
            "name": "Color",
            "type": "string",
            "variations": [
                {"name": "blue", "value": "blue"},
                {"name": "red", "value": "red"},
            ],
            "off_variation": "blue",
            "fallthrough": "red",
        }
        resp = await client.post("/api/flags", json=payload)
        assert resp.status_code == 201
        assert resp.json()["type"] == "string"

    async def test_create_with_targeting_rule(self, client):
        payload = _bool_flag_payload()
        payload["rules"] = [
            {
                "clauses": [{"attribute": "role", "operator": "is", "values": ["admin"]}],
                "variation": "on",
            }
        ]
        resp = await client.post("/api/flags", json=payload)
        assert resp.status_code == 201
        assert len(resp.json()["rules"]) == 1


# ---------------------------------------------------------------------------
# Flag API — get
# ---------------------------------------------------------------------------


class TestGetFlag:
    async def test_get_existing(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.get("/api/flags/my_flag")
        assert resp.status_code == 200
        assert resp.json()["key"] == "my_flag"

    async def test_get_missing_returns_404(self, client):
        resp = await client.get("/api/flags/nonexistent")
        assert resp.status_code == 404

    async def test_get_returns_full_model(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.get("/api/flags/my_flag")
        data = resp.json()
        assert "variations" in data
        assert "off_variation" in data
        assert "fallthrough" in data
        assert "enabled" in data


# ---------------------------------------------------------------------------
# Flag API — update
# ---------------------------------------------------------------------------


class TestUpdateFlag:
    async def test_put_updates_flag(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        updated = _bool_flag_payload()
        updated["name"] = "Updated Name"
        resp = await client.put("/api/flags/my_flag", json=updated)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    async def test_put_creates_if_missing(self, client):
        # PUT is an upsert — creates if not exists
        resp = await client.put("/api/flags/new_flag", json=_bool_flag_payload("new_flag"))
        assert resp.status_code == 200
        assert resp.json()["key"] == "new_flag"

    async def test_put_key_mismatch_returns_400(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.put(
            "/api/flags/my_flag",
            json=_bool_flag_payload("other_key"),
        )
        assert resp.status_code == 400

    async def test_put_without_key_in_body_uses_url_key(self, client):
        payload = _bool_flag_payload()
        payload.pop("key")
        resp = await client.put("/api/flags/my_flag", json=payload)
        assert resp.status_code == 200
        assert resp.json()["key"] == "my_flag"


# ---------------------------------------------------------------------------
# Flag API — enable / disable
# ---------------------------------------------------------------------------


class TestEnableDisableFlag:
    async def test_enable_flag(self, client):
        payload = _bool_flag_payload(enabled=False)
        await client.post("/api/flags", json=payload)

        resp = await client.post("/api/flags/my_flag/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    async def test_disable_flag(self, client):
        await client.post("/api/flags", json=_bool_flag_payload(enabled=True))

        resp = await client.post("/api/flags/my_flag/disable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_enable_missing_returns_404(self, client):
        resp = await client.post("/api/flags/nonexistent/enable")
        assert resp.status_code == 404

    async def test_disable_missing_returns_404(self, client):
        resp = await client.post("/api/flags/nonexistent/disable")
        assert resp.status_code == 404

    async def test_enable_updates_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_enable_cache")
        await client.post("/api/flags", json=_bool_flag_payload(enabled=False))
        await client.post("/api/flags/my_flag/enable")
        flag = engine._flag_provider._flags.get("my_flag")
        assert flag is not None
        assert flag.enabled is True


# ---------------------------------------------------------------------------
# Flag API — delete
# ---------------------------------------------------------------------------


class TestDeleteFlag:
    async def test_delete_existing(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.delete("/api/flags/my_flag")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "my_flag"

    async def test_delete_removes_from_list(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        await client.delete("/api/flags/my_flag")
        resp = await client.get("/api/flags")
        assert resp.json() == []

    async def test_delete_missing_returns_404(self, client):
        resp = await client.delete("/api/flags/nonexistent")
        assert resp.status_code == 404

    async def test_delete_updates_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_delete_cache")
        await client.post("/api/flags", json=_bool_flag_payload())
        await client.delete("/api/flags/my_flag")
        assert "my_flag" not in engine._flag_provider._flags


# ---------------------------------------------------------------------------
# Flag API — evaluate (debug endpoint)
# ---------------------------------------------------------------------------


class TestEvaluateFlag:
    async def test_evaluate_fallthrough(self, client):
        payload = _bool_flag_payload()
        payload["fallthrough"] = "on"
        await client.post("/api/flags", json=payload)

        resp = await client.post(
            "/api/flags/my_flag/evaluate",
            json={"context": {"key": "user_1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] is True
        assert data["reason"] == "FALLTHROUGH"

    async def test_evaluate_disabled_flag(self, client):
        await client.post("/api/flags", json=_bool_flag_payload(enabled=False))

        resp = await client.post(
            "/api/flags/my_flag/evaluate",
            json={"context": {"key": "user_1"}},
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "OFF"

    async def test_evaluate_with_targeting_rule(self, client):
        payload = _bool_flag_payload()
        payload["rules"] = [
            {
                "clauses": [{"attribute": "role", "operator": "is", "values": ["admin"]}],
                "variation": "on",
            }
        ]
        payload["fallthrough"] = "off"
        await client.post("/api/flags", json=payload)

        resp = await client.post(
            "/api/flags/my_flag/evaluate",
            json={"context": {"key": "user_1", "attributes": {"role": "admin"}}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] is True
        assert data["reason"] == "RULE_MATCH"

    async def test_evaluate_missing_flag_returns_404(self, client):
        resp = await client.post(
            "/api/flags/nonexistent/evaluate",
            json={"context": {"key": "user_1"}},
        )
        assert resp.status_code == 404

    async def test_evaluate_no_context_uses_anonymous(self, client):
        payload = _bool_flag_payload()
        payload["fallthrough"] = "on"
        await client.post("/api/flags", json=payload)

        resp = await client.post("/api/flags/my_flag/evaluate", json={})
        assert resp.status_code == 200

    async def test_evaluate_returns_all_fields(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.post(
            "/api/flags/my_flag/evaluate",
            json={"context": {"key": "u1"}},
        )
        data = resp.json()
        assert "flag_key" in data
        assert "value" in data
        assert "variation" in data
        assert "reason" in data
        assert "rule_id" in data
        assert "prerequisite_key" in data


# ---------------------------------------------------------------------------
# Segment API — list
# ---------------------------------------------------------------------------


class TestListSegments:
    async def test_empty(self, client):
        resp = await client.get("/api/segments")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_saved_segment(self, client, engine):
        seg = Segment.model_validate(_segment_payload())
        await engine.save_segment(seg)

        resp = await client.get("/api/segments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "beta"


# ---------------------------------------------------------------------------
# Segment API — create
# ---------------------------------------------------------------------------


class TestCreateSegment:
    async def test_create_returns_201(self, client):
        resp = await client.post("/api/segments", json=_segment_payload())
        assert resp.status_code == 201
        assert resp.json()["key"] == "beta"

    async def test_create_conflict_returns_409(self, client):
        await client.post("/api/segments", json=_segment_payload())
        resp = await client.post("/api/segments", json=_segment_payload())
        assert resp.status_code == 409

    async def test_create_with_rules(self, client):
        payload = _segment_payload()
        payload["rules"] = [
            {"clauses": [{"attribute": "plan", "operator": "is", "values": ["pro"]}]}
        ]
        resp = await client.post("/api/segments", json=payload)
        assert resp.status_code == 201
        assert len(resp.json()["rules"]) == 1

    async def test_create_invalid_body_returns_400(self, client):
        resp = await client.post("/api/segments", json={"bad": "data"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Segment API — get
# ---------------------------------------------------------------------------


class TestGetSegment:
    async def test_get_existing(self, client):
        await client.post("/api/segments", json=_segment_payload())
        resp = await client.get("/api/segments/beta")
        assert resp.status_code == 200
        assert resp.json()["key"] == "beta"

    async def test_get_missing_returns_404(self, client):
        resp = await client.get("/api/segments/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Segment API — update
# ---------------------------------------------------------------------------


class TestUpdateSegment:
    async def test_put_updates_segment(self, client):
        await client.post("/api/segments", json=_segment_payload())
        updated = _segment_payload()
        updated["name"] = "Updated Beta"
        resp = await client.put("/api/segments/beta", json=updated)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Beta"

    async def test_put_key_mismatch_returns_400(self, client):
        await client.post("/api/segments", json=_segment_payload())
        resp = await client.put(
            "/api/segments/beta",
            json=_segment_payload("other"),
        )
        assert resp.status_code == 400

    async def test_put_without_key_uses_url_key(self, client):
        payload = _segment_payload()
        payload.pop("key")
        resp = await client.put("/api/segments/beta", json=payload)
        assert resp.status_code == 200
        assert resp.json()["key"] == "beta"


# ---------------------------------------------------------------------------
# Segment API — delete
# ---------------------------------------------------------------------------


class TestDeleteSegment:
    async def test_delete_existing(self, client):
        await client.post("/api/segments", json=_segment_payload())
        resp = await client.delete("/api/segments/beta")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "beta"

    async def test_delete_removes_from_list(self, client):
        await client.post("/api/segments", json=_segment_payload())
        await client.delete("/api/segments/beta")
        resp = await client.get("/api/segments")
        assert resp.json() == []

    async def test_delete_missing_returns_404(self, client):
        resp = await client.delete("/api/segments/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Provider cache sync
# ---------------------------------------------------------------------------


class TestProviderCacheSync:
    """Verify that REST operations propagate into the provider's in-memory cache."""

    async def test_create_flag_populates_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_create_sync")
        await client.post("/api/flags", json=_bool_flag_payload())
        assert "my_flag" in engine._flag_provider._flags

    async def test_delete_flag_removes_from_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_del_sync")
        await client.post("/api/flags", json=_bool_flag_payload())
        await client.delete("/api/flags/my_flag")
        assert "my_flag" not in engine._flag_provider._flags

    async def test_create_segment_populates_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_seg_sync")
        await client.post("/api/segments", json=_segment_payload())
        assert "beta" in engine._flag_provider._segments

    async def test_delete_segment_removes_from_provider_cache(self, client, engine):
        engine.use_openfeature(domain="test_seg_del_sync")
        await client.post("/api/segments", json=_segment_payload())
        await client.delete("/api/segments/beta")
        assert "beta" not in engine._flag_provider._segments


# ---------------------------------------------------------------------------
# Evaluate with segment targeting
# ---------------------------------------------------------------------------


class TestEvaluateWithSegment:
    async def test_segment_rule_resolves_correctly(self, client, engine):
        # Create a segment
        await client.post(
            "/api/segments",
            json={
                "key": "pro_users",
                "name": "Pro Users",
                "rules": [
                    {"clauses": [{"attribute": "plan", "operator": "is", "values": ["pro"]}]}
                ],
            },
        )

        # Create a flag that targets the segment
        payload = _bool_flag_payload()
        payload["fallthrough"] = "off"
        payload["rules"] = [
            {
                "clauses": [{"attribute": "", "operator": "in_segment", "values": ["pro_users"]}],
                "variation": "on",
            }
        ]
        await client.post("/api/flags", json=payload)

        resp = await client.post(
            "/api/flags/my_flag/evaluate",
            json={"context": {"key": "user_1", "attributes": {"plan": "pro"}}},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] is True


# ---------------------------------------------------------------------------
# Flag API — PATCH (partial update / LaunchDarkly-style mutation)
# ---------------------------------------------------------------------------


class TestPatchFlag:
    async def test_patch_name(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"name": "Renamed Flag"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed Flag"
        # Other fields untouched
        assert resp.json()["type"] == "boolean"
        assert resp.json()["key"] == "my_flag"

    async def test_patch_persists(self, client, engine):
        await client.post("/api/flags", json=_bool_flag_payload())
        await client.patch("/api/flags/my_flag", json={"name": "Persisted"})
        flag = await engine.get_flag("my_flag")
        assert flag.name == "Persisted"

    async def test_patch_description(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"description": "hello"})
        assert resp.status_code == 200
        assert resp.json()["description"] == "hello"

    async def test_patch_off_variation(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"off_variation": "on"})
        assert resp.status_code == 200
        assert resp.json()["off_variation"] == "on"

    async def test_patch_fallthrough(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"fallthrough": "on"})
        assert resp.status_code == 200
        assert resp.json()["fallthrough"] == "on"

    async def test_patch_ignores_key(self, client):
        """key must be immutable — PATCH body key field is silently ignored."""
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"key": "HACKED", "name": "ok"})
        assert resp.status_code == 200
        assert resp.json()["key"] == "my_flag"

    async def test_patch_ignores_type(self, client):
        """type must be immutable — PATCH body type field is silently ignored."""
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", json={"type": "string", "name": "ok"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "boolean"

    async def test_patch_missing_flag_returns_404(self, client):
        resp = await client.patch("/api/flags/no_such_flag", json={"name": "x"})
        assert resp.status_code == 404

    async def test_patch_invalid_body_returns_400(self, client):
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch("/api/flags/my_flag", content=b"not json")
        assert resp.status_code == 400

    async def test_patch_invalid_variation_name_returns_400(self, client):
        """off_variation must reference an existing variation name."""
        await client.post("/api/flags", json=_bool_flag_payload())
        resp = await client.patch(
            "/api/flags/my_flag",
            json={"off_variation": "nonexistent_variation"},
        )
        assert resp.status_code == 400

    async def test_patch_replaces_rules(self, client, engine):
        await client.post("/api/flags", json=_bool_flag_payload())
        new_rules = [
            {
                "clauses": [{"attribute": "plan", "operator": "is", "values": ["pro"]}],
                "variation": "on",
            }
        ]
        resp = await client.patch("/api/flags/my_flag", json={"rules": new_rules})
        assert resp.status_code == 200
        flag = await engine.get_flag("my_flag")
        assert len(flag.rules) == 1
        assert flag.rules[0].clauses[0].attribute == "plan"
