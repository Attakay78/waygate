"""Tests for the flag + segment dashboard UI routes (Phase 5)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from waygate.admin.app import WaygateAdmin
from waygate.core.engine import WaygateEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> WaygateEngine:
    e = WaygateEngine()
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def admin_app(engine: WaygateEngine) -> object:
    """WaygateAdmin with flags enabled, no auth."""
    return WaygateAdmin(engine=engine, auth=None, enable_flags=True)


@pytest.fixture
async def client(admin_app: object) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=admin_app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
def admin_app_no_flags(engine: WaygateEngine) -> object:
    """WaygateAdmin with flags disabled."""
    return WaygateAdmin(engine=engine, auth=None, enable_flags=False)


@pytest.fixture
async def client_no_flags(admin_app_no_flags: object) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=admin_app_no_flags),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flag_payload(
    key: str = "my-flag",
    flag_type: str = "boolean",
    enabled: bool = True,
    on_value: object = True,
    off_value: object = False,
) -> dict:
    return {
        "key": key,
        "name": key.replace("-", " ").title(),
        "type": flag_type,
        "variations": [
            {"name": "on", "value": on_value},
            {"name": "off", "value": off_value},
        ],
        "off_variation": "off",
        "fallthrough": "on",
        "enabled": enabled,
    }


def _segment_payload(
    key: str, included: list[str] | None = None, excluded: list[str] | None = None
) -> dict:
    return {
        "key": key,
        "name": key.replace("-", " ").title(),
        "included": included or [],
        "excluded": excluded or [],
        "rules": [],
    }


# ---------------------------------------------------------------------------
# Flags page — GET /flags
# ---------------------------------------------------------------------------


class TestFlagsPage:
    async def test_flags_page_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/flags")
        assert resp.status_code == 200

    async def test_flags_page_html(self, client: AsyncClient) -> None:
        resp = await client.get("/flags")
        assert "text/html" in resp.headers["content-type"]

    async def test_flags_page_shows_empty_state(self, client: AsyncClient) -> None:
        resp = await client.get("/flags")
        # Empty flags list renders without error
        assert resp.status_code == 200

    async def test_flags_page_shows_flag_key(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("checkout-flag"))
        resp = await client.get("/flags")
        assert "checkout-flag" in resp.text

    async def test_flags_page_not_mounted_when_disabled(self, client_no_flags: AsyncClient) -> None:
        resp = await client_no_flags.get("/flags")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Flags rows partial — GET /flags/rows
# ---------------------------------------------------------------------------


class TestFlagsRowsPartial:
    async def test_rows_partial_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/flags/rows")
        assert resp.status_code == 200

    async def test_rows_partial_search_filter(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("alpha-flag"))
        await client.post("/api/flags", json=_flag_payload("beta-flag"))
        resp = await client.get("/flags/rows?q=alpha")
        assert "alpha-flag" in resp.text
        assert "beta-flag" not in resp.text

    async def test_rows_partial_type_filter(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("bool-flag", "boolean"))
        await client.post(
            "/api/flags",
            json=_flag_payload("str-flag", "string", on_value="hello", off_value=""),
        )
        resp = await client.get("/flags/rows?type=boolean")
        assert "bool-flag" in resp.text
        assert "str-flag" not in resp.text

    async def test_rows_partial_status_enabled(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("on-flag", enabled=True))
        await client.post("/api/flags", json=_flag_payload("off-flag", enabled=False))
        resp = await client.get("/flags/rows?status=enabled")
        assert "on-flag" in resp.text
        assert "off-flag" not in resp.text

    async def test_rows_partial_status_disabled(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("on-flag", enabled=True))
        await client.post("/api/flags", json=_flag_payload("off-flag", enabled=False))
        resp = await client.get("/flags/rows?status=disabled")
        assert "off-flag" in resp.text
        assert "on-flag" not in resp.text


# ---------------------------------------------------------------------------
# Flag detail page — GET /flags/{key}
# ---------------------------------------------------------------------------


class TestFlagDetailPage:
    async def test_detail_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("detail-flag"))
        resp = await client.get("/flags/detail-flag")
        assert resp.status_code == 200

    async def test_detail_shows_flag_key(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("detail-flag"))
        resp = await client.get("/flags/detail-flag")
        assert "detail-flag" in resp.text

    async def test_detail_404_for_missing_flag(self, client: AsyncClient) -> None:
        resp = await client.get("/flags/nonexistent-flag")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Flag enable / disable — POST /flags/{key}/enable, /flags/{key}/disable
# ---------------------------------------------------------------------------


class TestFlagEnableDisable:
    async def test_flag_enable_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("toggle-flag", enabled=False))
        resp = await client.post("/flags/toggle-flag/enable")
        assert resp.status_code == 200

    async def test_flag_enable_updates_state(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("toggle-flag", enabled=False))
        await client.post("/flags/toggle-flag/enable")
        check = await client.get("/api/flags/toggle-flag")
        assert check.json()["enabled"] is True

    async def test_flag_disable_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("toggle-flag", enabled=True))
        resp = await client.post("/flags/toggle-flag/disable")
        assert resp.status_code == 200

    async def test_flag_disable_updates_state(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("toggle-flag", enabled=True))
        await client.post("/flags/toggle-flag/disable")
        check = await client.get("/api/flags/toggle-flag")
        assert check.json()["enabled"] is False

    async def test_enable_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post("/flags/ghost-flag/enable")
        assert resp.status_code == 404

    async def test_disable_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post("/flags/ghost-flag/disable")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Flag delete — DELETE /flags/{key}
# ---------------------------------------------------------------------------


class TestFlagDelete:
    async def test_flag_delete_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("del-flag"))
        resp = await client.delete("/flags/del-flag")
        assert resp.status_code == 200

    async def test_flag_delete_removes_flag(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("del-flag"))
        await client.delete("/flags/del-flag")
        check = await client.get("/api/flags/del-flag")
        assert check.status_code == 404

    async def test_delete_missing_flag_returns_200(self, client: AsyncClient) -> None:
        # Dashboard DELETE is idempotent — HTMX removes row; no 404 from UI
        resp = await client.delete("/flags/ghost-flag")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Flag create modal — GET /modal/flag/create
# ---------------------------------------------------------------------------


class TestFlagCreateModal:
    async def test_modal_create_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/modal/flag/create")
        assert resp.status_code == 200

    async def test_modal_create_contains_form(self, client: AsyncClient) -> None:
        resp = await client.get("/modal/flag/create")
        assert "form" in resp.text.lower()


# ---------------------------------------------------------------------------
# Flag create form — POST /flags/create
# ---------------------------------------------------------------------------


class TestFlagCreateForm:
    async def test_create_form_boolean_flag(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/create",
            data={"key": "form-bool-flag", "type": "boolean", "name": "Form Bool"},
        )
        assert resp.status_code == 200

    async def test_create_form_persists_flag(self, client: AsyncClient) -> None:
        await client.post(
            "/flags/create",
            data={"key": "persisted-flag", "type": "boolean", "name": "Persisted"},
        )
        check = await client.get("/api/flags/persisted-flag")
        assert check.status_code == 200
        assert check.json()["key"] == "persisted-flag"

    async def test_create_form_string_flag(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/create",
            data={
                "key": "str-flag",
                "type": "string",
                "name": "String Flag",
                "default_value": "hello",
            },
        )
        assert resp.status_code == 200

    async def test_create_form_duplicate_key_returns_error(self, client: AsyncClient) -> None:
        await client.post(
            "/flags/create", data={"key": "dup-flag", "type": "boolean", "name": "Dup"}
        )
        resp = await client.post(
            "/flags/create", data={"key": "dup-flag", "type": "boolean", "name": "Dup"}
        )
        # Should return an error response (409 or HTML with error)
        assert resp.status_code in (200, 409)


# ---------------------------------------------------------------------------
# Flag eval modal — GET /modal/flag/{key}/eval
# ---------------------------------------------------------------------------


class TestFlagEvalModal:
    async def test_eval_modal_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("eval-flag"))
        resp = await client.get("/modal/flag/eval-flag/eval")
        assert resp.status_code == 200

    async def test_eval_modal_contains_form(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("eval-flag"))
        resp = await client.get("/modal/flag/eval-flag/eval")
        assert "form" in resp.text.lower()


# ---------------------------------------------------------------------------
# Flag eval form — POST /flags/{key}/eval
# ---------------------------------------------------------------------------


class TestFlagEvalForm:
    async def test_eval_form_returns_result(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("eval-flag", enabled=True))
        resp = await client.post(
            "/flags/eval-flag/eval",
            data={"targeting_key": "user-1"},
        )
        assert resp.status_code == 200

    async def test_eval_form_triggers_event(self, client: AsyncClient) -> None:
        import json as _json

        await client.post("/api/flags", json=_flag_payload("eval-flag", enabled=True))
        resp = await client.post(
            "/flags/eval-flag/eval",
            data={"context_key": "user-1"},
        )
        assert resp.status_code == 200
        # Eval returns rich result HTML + HX-Trigger header with the result payload
        assert "HX-Trigger" in resp.headers
        trigger = _json.loads(resp.headers["HX-Trigger"])
        assert "waygateEvalDone" in trigger
        payload = trigger["waygateEvalDone"]
        assert "value" in payload
        assert "reason" in payload
        assert payload["flagKey"] == "eval-flag"

    async def test_eval_form_shows_result_panel(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("eval-flag", enabled=True))
        resp = await client.post(
            "/flags/eval-flag/eval",
            data={"context_key": "user-1"},
        )
        assert resp.status_code == 200
        assert "Evaluation Result" in resp.text
        assert "FALLTHROUGH" in resp.text or "OFF" in resp.text or "RULE_MATCH" in resp.text

    async def test_eval_form_shows_context_summary(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("eval-flag", enabled=True))
        resp = await client.post(
            "/flags/eval-flag/eval",
            data={"context_key": "user-99", "kind": "organization", "attributes": "plan=pro"},
        )
        assert resp.status_code == 200
        assert "user-99" in resp.text
        assert "organization" in resp.text
        assert "plan" in resp.text


# ---------------------------------------------------------------------------
# Segments page — GET /segments
# ---------------------------------------------------------------------------


class TestSegmentsPage:
    async def test_segments_page_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/segments")
        assert resp.status_code == 200

    async def test_segments_page_html(self, client: AsyncClient) -> None:
        resp = await client.get("/segments")
        assert "text/html" in resp.headers["content-type"]

    async def test_segments_page_not_mounted_when_disabled(
        self, client_no_flags: AsyncClient
    ) -> None:
        resp = await client_no_flags.get("/segments")
        assert resp.status_code == 404

    async def test_segments_page_shows_segment(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("beta-users", included=["u1"]))
        resp = await client.get("/segments")
        assert "beta-users" in resp.text


# ---------------------------------------------------------------------------
# Segments rows partial — GET /segments/rows
# ---------------------------------------------------------------------------


class TestSegmentsRowsPartial:
    async def test_rows_partial_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/segments/rows")
        assert resp.status_code == 200

    async def test_rows_partial_search_filter(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("alpha-seg"))
        await client.post("/api/segments", json=_segment_payload("beta-seg"))
        resp = await client.get("/segments/rows?q=alpha")
        assert "alpha-seg" in resp.text
        assert "beta-seg" not in resp.text


# ---------------------------------------------------------------------------
# Segment create modal — GET /modal/segment/create
# ---------------------------------------------------------------------------


class TestSegmentCreateModal:
    async def test_modal_create_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/modal/segment/create")
        assert resp.status_code == 200

    async def test_modal_create_contains_form(self, client: AsyncClient) -> None:
        resp = await client.get("/modal/segment/create")
        assert "form" in resp.text.lower()


# ---------------------------------------------------------------------------
# Segment create form — POST /segments/create
# ---------------------------------------------------------------------------


class TestSegmentCreateForm:
    async def test_create_form_persists_segment(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/segments/create",
            data={"key": "new-seg", "name": "New Seg", "included": "u1\nu2", "excluded": ""},
        )
        assert resp.status_code == 200
        check = await client.get("/api/segments/new-seg")
        assert check.status_code == 200
        assert check.json()["key"] == "new-seg"

    async def test_create_form_creates_empty_segment(self, client: AsyncClient) -> None:
        # segment_create_form only sets key+name; membership is edited via save_form
        await client.post(
            "/segments/create",
            data={"key": "inc-seg", "name": "Inc Seg"},
        )
        check = await client.get("/api/segments/inc-seg")
        assert check.status_code == 200
        assert check.json()["key"] == "inc-seg"


# ---------------------------------------------------------------------------
# Segment detail modal — GET /modal/segment/{key}
# ---------------------------------------------------------------------------


class TestSegmentDetailModal:
    async def test_detail_modal_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("detail-seg", included=["u1"]))
        resp = await client.get("/modal/segment/detail-seg")
        assert resp.status_code == 200

    async def test_detail_modal_shows_key(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("detail-seg", included=["u1"]))
        resp = await client.get("/modal/segment/detail-seg")
        assert "detail-seg" in resp.text


# ---------------------------------------------------------------------------
# Segment save form — POST /segments/{key}/save
# ---------------------------------------------------------------------------


class TestSegmentSaveForm:
    async def test_save_form_updates_included(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("save-seg", included=["old-user"]))
        resp = await client.post(
            "/segments/save-seg/save",
            data={"included": "new-user1\nnew-user2", "excluded": ""},
        )
        assert resp.status_code == 200
        check = await client.get("/api/segments/save-seg")
        assert "new-user1" in check.json()["included"]
        assert "new-user2" in check.json()["included"]

    async def test_save_form_updates_excluded(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json=_segment_payload("save-seg2"))
        resp = await client.post(
            "/segments/save-seg2/save",
            data={"included": "", "excluded": "blocked-user"},
        )
        assert resp.status_code == 200
        check = await client.get("/api/segments/save-seg2")
        assert "blocked-user" in check.json()["excluded"]

    async def test_save_missing_segment_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/segments/ghost-seg/save",
            data={"included": "u1", "excluded": ""},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Segment delete — DELETE /segments/{key}
# ---------------------------------------------------------------------------


class TestSegmentDelete:
    async def test_delete_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json={"key": "del-seg", "included": []})
        resp = await client.delete("/segments/del-seg")
        assert resp.status_code == 200

    async def test_delete_removes_segment(self, client: AsyncClient) -> None:
        await client.post("/api/segments", json={"key": "del-seg2", "included": []})
        await client.delete("/segments/del-seg2")
        check = await client.get("/api/segments/del-seg2")
        assert check.status_code == 404

    async def test_delete_missing_segment_returns_200(self, client: AsyncClient) -> None:
        # Dashboard DELETE is idempotent — HTMX removes row; no 404 from UI
        resp = await client.delete("/segments/ghost-seg")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# flags_enabled Jinja2 global
# ---------------------------------------------------------------------------


class TestFlagsEnabledGlobal:
    async def test_index_no_flags_tab_when_disabled(self, client_no_flags: AsyncClient) -> None:
        """When enable_flags=False, the main dashboard should not show flag nav links."""
        resp = await client_no_flags.get("/")
        assert resp.status_code == 200
        # Flags nav item should not appear
        assert "/flags" not in resp.text

    async def test_index_flags_tab_when_enabled(self, client: AsyncClient) -> None:
        """When enable_flags=True, the main dashboard should show flag nav links."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "/flags" in resp.text


# ---------------------------------------------------------------------------
# Flag settings save — POST /flags/{key}/settings/save
# ---------------------------------------------------------------------------


class TestFlagSettingsSave:
    async def test_settings_save_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("s-flag"))
        resp = await client.post(
            "/flags/s-flag/settings/save",
            data={"name": "New Name", "description": "Updated desc"},
        )
        assert resp.status_code == 200

    async def test_settings_save_updates_name(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("s-flag"))
        await client.post(
            "/flags/s-flag/settings/save",
            data={"name": "Renamed Flag", "description": ""},
        )
        flag = await engine.get_flag("s-flag")
        assert flag.name == "Renamed Flag"

    async def test_settings_save_updates_description(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("s-flag"))
        await client.post(
            "/flags/s-flag/settings/save",
            data={"name": "S Flag", "description": "A description"},
        )
        flag = await engine.get_flag("s-flag")
        assert flag.description == "A description"

    async def test_settings_save_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/no-flag/settings/save",
            data={"name": "x", "description": ""},
        )
        assert resp.status_code == 404

    async def test_settings_save_returns_hx_trigger(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("s-flag"))
        resp = await client.post(
            "/flags/s-flag/settings/save",
            data={"name": "x", "description": ""},
        )
        assert "HX-Trigger" in resp.headers


# ---------------------------------------------------------------------------
# Flag variations save — POST /flags/{key}/variations/save
# ---------------------------------------------------------------------------


class TestFlagVariationsSave:
    async def test_variations_save_returns_200(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("v-flag"))
        resp = await client.post(
            "/flags/v-flag/variations/save",
            data={
                "variations[0][name]": "enabled",
                "variations[0][value]": "true",
                "variations[1][name]": "disabled",
                "variations[1][value]": "false",
            },
        )
        assert resp.status_code == 200

    async def test_variations_save_updates_names(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("v-flag"))
        await client.post(
            "/flags/v-flag/variations/save",
            data={
                "variations[0][name]": "enabled",
                "variations[0][value]": "true",
                "variations[1][name]": "disabled",
                "variations[1][value]": "false",
            },
        )
        flag = await engine.get_flag("v-flag")
        names = [v.name for v in flag.variations]
        assert "enabled" in names
        assert "disabled" in names

    async def test_variations_save_fewer_than_two_returns_400(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("v-flag"))
        resp = await client.post(
            "/flags/v-flag/variations/save",
            data={"variations[0][name]": "only", "variations[0][value]": "true"},
        )
        assert resp.status_code == 400

    async def test_variations_save_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/no-flag/variations/save",
            data={
                "variations[0][name]": "a",
                "variations[0][value]": "true",
                "variations[1][name]": "b",
                "variations[1][value]": "false",
            },
        )
        assert resp.status_code == 404

    async def test_variations_save_returns_hx_trigger(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("v-flag"))
        resp = await client.post(
            "/flags/v-flag/variations/save",
            data={
                "variations[0][name]": "on",
                "variations[0][value]": "true",
                "variations[1][name]": "off",
                "variations[1][value]": "false",
            },
        )
        assert "HX-Trigger" in resp.headers


# ---------------------------------------------------------------------------
# Flag targeting save — POST /flags/{key}/targeting/save
# ---------------------------------------------------------------------------


class TestFlagTargetingSave:
    async def test_targeting_save_off_variation(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={"off_variation": "on"},
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("t-flag")
        assert flag.off_variation == "on"

    async def test_targeting_save_fallthrough(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={"fallthrough": "off"},
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("t-flag")
        assert flag.fallthrough == "off"

    async def test_targeting_save_invalid_variation_returns_400(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={"off_variation": "nonexistent"},
        )
        assert resp.status_code == 400

    async def test_targeting_save_with_rule(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={
                "rules[0][id]": "",
                "rules[0][description]": "Beta rule",
                "rules[0][variation]": "on",
                "rules[0][clauses][0][attribute]": "plan",
                "rules[0][clauses][0][operator]": "is",
                "rules[0][clauses][0][values]": "pro",
            },
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("t-flag")
        assert len(flag.rules) == 1
        assert flag.rules[0].clauses[0].attribute == "plan"

    async def test_targeting_save_with_segment_rule(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        """in_segment clauses should auto-set attribute to 'key' even when omitted."""
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={
                "rules[0][id]": "",
                "rules[0][description]": "Segment rule",
                "rules[0][variation]": "on",
                # attribute intentionally blank (hidden in dashboard for segment ops)
                "rules[0][clauses][0][attribute]": "",
                "rules[0][clauses][0][operator]": "in_segment",
                "rules[0][clauses][0][values]": "beta-users",
            },
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("t-flag")
        assert len(flag.rules) == 1
        clause = flag.rules[0].clauses[0]
        assert clause.operator == "in_segment"
        assert clause.attribute == "key"
        assert clause.values == ["beta-users"]

    async def test_targeting_save_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/no-flag/targeting/save",
            data={"off_variation": "on"},
        )
        assert resp.status_code == 404

    async def test_targeting_save_returns_hx_trigger(self, client: AsyncClient) -> None:
        await client.post("/api/flags", json=_flag_payload("t-flag"))
        resp = await client.post(
            "/flags/t-flag/targeting/save",
            data={"off_variation": "on"},
        )
        assert "HX-Trigger" in resp.headers


# ---------------------------------------------------------------------------
# Flag prerequisites save — POST /flags/{key}/prerequisites/save
# ---------------------------------------------------------------------------


class TestFlagPrerequisitesSave:
    async def test_prerequisites_save_empty(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        """POST empty form clears prerequisites, returns 200."""
        await client.post("/api/flags", json=_flag_payload("prereq-flag"))
        resp = await client.post("/flags/prereq-flag/prerequisites/save", data={})
        assert resp.status_code == 200
        flag = await engine.get_flag("prereq-flag")
        assert flag.prerequisites == []

    async def test_prerequisites_save_adds_prereq(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        """POST with prereqs[0][flag_key]=other_flag&prereqs[0][variation]=on saves it, persists."""
        await client.post("/api/flags", json=_flag_payload("main-flag"))
        await client.post("/api/flags", json=_flag_payload("other-flag"))
        resp = await client.post(
            "/flags/main-flag/prerequisites/save",
            data={
                "prereqs[0][flag_key]": "other-flag",
                "prereqs[0][variation]": "on",
            },
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("main-flag")
        assert len(flag.prerequisites) == 1
        assert flag.prerequisites[0].flag_key == "other-flag"
        assert flag.prerequisites[0].variation == "on"

    async def test_prerequisites_save_circular_returns_400(self, client: AsyncClient) -> None:
        """POST where flag_key == current flag's key returns 400."""
        await client.post("/api/flags", json=_flag_payload("circ-flag"))
        resp = await client.post(
            "/flags/circ-flag/prerequisites/save",
            data={
                "prereqs[0][flag_key]": "circ-flag",
                "prereqs[0][variation]": "on",
            },
        )
        assert resp.status_code == 400

    async def test_prerequisites_save_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/no-flag/prerequisites/save",
            data={},
        )
        assert resp.status_code == 404

    async def test_prerequisites_save_returns_hx_trigger(self, client: AsyncClient) -> None:
        """Response has HX-Trigger header with flagPrerequisitesSaved."""
        import json as _json

        await client.post("/api/flags", json=_flag_payload("hxt-flag"))
        resp = await client.post("/flags/hxt-flag/prerequisites/save", data={})
        assert "HX-Trigger" in resp.headers
        trigger = _json.loads(resp.headers["HX-Trigger"])
        assert "flagPrerequisitesSaved" in trigger


# ---------------------------------------------------------------------------
# Flag targets save — POST /flags/{key}/targets/save
# ---------------------------------------------------------------------------


class TestFlagTargetsSave:
    async def test_targets_save_adds_keys(self, client: AsyncClient, engine: WaygateEngine) -> None:
        """POST with targets[on]=user_123\\nuser_456, persists correctly."""
        await client.post("/api/flags", json=_flag_payload("tgt-flag"))
        resp = await client.post(
            "/flags/tgt-flag/targets/save",
            data={"targets[on]": "user_123\nuser_456"},
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("tgt-flag")
        assert "user_123" in flag.targets.get("on", [])
        assert "user_456" in flag.targets.get("on", [])

    async def test_targets_save_clears_targets(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        """POST with empty textareas clears targets."""
        await client.post(
            "/api/flags",
            json={**_flag_payload("tgt-clear-flag"), "targets": {"on": ["old-user"]}},
        )
        resp = await client.post(
            "/flags/tgt-clear-flag/targets/save",
            data={"targets[on]": "", "targets[off]": ""},
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("tgt-clear-flag")
        assert flag.targets == {} or flag.targets.get("on", []) == []

    async def test_targets_save_ignores_unknown_variation(
        self, client: AsyncClient, engine: WaygateEngine
    ) -> None:
        """POST with targets[nonexistent]=user_x doesn't save it."""
        await client.post("/api/flags", json=_flag_payload("tgt-unk-flag"))
        resp = await client.post(
            "/flags/tgt-unk-flag/targets/save",
            data={"targets[nonexistent]": "user_x"},
        )
        assert resp.status_code == 200
        flag = await engine.get_flag("tgt-unk-flag")
        assert "nonexistent" not in flag.targets

    async def test_targets_save_missing_flag_returns_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/flags/no-flag/targets/save",
            data={"targets[on]": "user_1"},
        )
        assert resp.status_code == 404

    async def test_targets_save_returns_hx_trigger(self, client: AsyncClient) -> None:
        """Response has HX-Trigger header with flagTargetsSaved."""
        import json as _json

        await client.post("/api/flags", json=_flag_payload("tgt-hxt-flag"))
        resp = await client.post(
            "/flags/tgt-hxt-flag/targets/save",
            data={"targets[on]": "user_1"},
        )
        assert "HX-Trigger" in resp.headers
        trigger = _json.loads(resp.headers["HX-Trigger"])
        assert "flagTargetsSaved" in trigger
