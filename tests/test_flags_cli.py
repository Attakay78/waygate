"""Tests for the waygate flags / waygate segments CLI commands.

Tests are sync (def, not async) — the CLI uses anyio.run() internally
and cannot be nested inside a running pytest-asyncio event loop.

Pattern: create an in-process WaygateAdmin, inject it via make_client mock,
invoke CLI commands through typer.testing.CliRunner.
"""

from __future__ import annotations

from unittest.mock import patch

import anyio
import httpx
from typer.testing import CliRunner

from waygate.admin.app import WaygateAdmin
from waygate.cli.client import WaygateClient
from waygate.cli.main import cli as app
from waygate.core.engine import WaygateEngine
from waygate.core.feature_flags.models import (
    FeatureFlag,
    FlagType,
    FlagVariation,
    Segment,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_engine_with_flags(*flags: FeatureFlag, segments: list = None) -> WaygateEngine:
    """Create engine, seed flags and segments synchronously."""
    e = WaygateEngine()

    async def _run() -> None:
        for flag in flags:
            await e.save_flag(flag)
        for seg in segments or []:
            await e.save_segment(seg)

    anyio.run(_run)
    return e


def _make_flag(key: str = "my_flag", enabled: bool = True) -> FeatureFlag:
    return FeatureFlag(
        key=key,
        name="My Flag",
        type=FlagType.BOOLEAN,
        variations=[
            FlagVariation(name="on", value=True),
            FlagVariation(name="off", value=False),
        ],
        off_variation="off",
        fallthrough="on",
        enabled=enabled,
    )


def _make_segment(key: str = "beta", included: list[str] = None) -> Segment:
    return Segment(key=key, name="Beta Users", included=included or [])


def _open_client(engine: WaygateEngine) -> WaygateClient:
    """WaygateClient backed by in-process WaygateAdmin with flags enabled."""
    admin = WaygateAdmin(engine=engine, enable_flags=True)
    return WaygateClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=admin),
    )


def invoke(client: WaygateClient, *args: str):
    with patch("waygate.cli.main.make_client", return_value=client):
        return runner.invoke(app, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# waygate flags list
# ---------------------------------------------------------------------------


class TestFlagsList:
    def test_empty_shows_no_flags_message(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "list")
        assert result.exit_code == 0
        assert "No flags found" in result.output

    def test_shows_flag_row(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "list")
        assert result.exit_code == 0
        assert "my_flag" in result.output

    def test_shows_multiple_flags(self):
        engine = _seed_engine_with_flags(_make_flag("flag_a"), _make_flag("flag_b"))
        client = _open_client(engine)
        result = invoke(client, "flags", "list")
        assert result.exit_code == 0
        assert "flag_a" in result.output
        assert "flag_b" in result.output

    def test_filter_by_type(self):
        engine = _seed_engine_with_flags(_make_flag("bool_flag"))
        client = _open_client(engine)
        result = invoke(client, "flags", "list", "--type", "boolean")
        assert result.exit_code == 0
        assert "bool_flag" in result.output

    def test_filter_by_type_no_match(self):
        engine = _seed_engine_with_flags(_make_flag("bool_flag"))
        client = _open_client(engine)
        result = invoke(client, "flags", "list", "--type", "string")
        assert result.exit_code == 0
        assert "No flags found" in result.output

    def test_filter_status_enabled(self):
        engine = _seed_engine_with_flags(
            _make_flag("on_flag", enabled=True), _make_flag("off_flag", enabled=False)
        )
        client = _open_client(engine)
        result = invoke(client, "flags", "list", "--status", "enabled")
        assert result.exit_code == 0
        assert "on_flag" in result.output
        assert "off_flag" not in result.output

    def test_filter_status_disabled(self):
        engine = _seed_engine_with_flags(
            _make_flag("on_flag", enabled=True), _make_flag("off_flag", enabled=False)
        )
        client = _open_client(engine)
        result = invoke(client, "flags", "list", "--status", "disabled")
        assert result.exit_code == 0
        assert "off_flag" in result.output
        assert "on_flag" not in result.output

    def test_shows_count(self):
        engine = _seed_engine_with_flags(_make_flag("a"), _make_flag("b"), _make_flag("c"))
        client = _open_client(engine)
        result = invoke(client, "flags", "list")
        assert result.exit_code == 0
        assert "3 flag" in result.output


# ---------------------------------------------------------------------------
# waygate flags get
# ---------------------------------------------------------------------------


class TestFlagsGet:
    def test_shows_flag_details(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "get", "my_flag")
        assert result.exit_code == 0
        assert "my_flag" in result.output
        assert "boolean" in result.output

    def test_shows_variations(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "get", "my_flag")
        assert result.exit_code == 0
        assert "on" in result.output
        assert "off" in result.output

    def test_shows_enabled_status(self):
        engine = _seed_engine_with_flags(_make_flag(enabled=True))
        client = _open_client(engine)
        result = invoke(client, "flags", "get", "my_flag")
        assert result.exit_code == 0
        assert "enabled" in result.output

    def test_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "get", "nonexistent")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags create
# ---------------------------------------------------------------------------


class TestFlagsCreate:
    def test_create_boolean_flag(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "create", "my_flag", "--name", "My Flag")
        assert result.exit_code == 0
        assert "created" in result.output.lower()
        assert "my_flag" in result.output

    def test_create_string_flag(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client, "flags", "create", "color_flag", "--name", "Color", "--type", "string"
        )
        assert result.exit_code == 0
        assert "color_flag" in result.output

    def test_create_persists_flag(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        invoke(client, "flags", "create", "persist_me", "--name", "Persist")

        flags_result = invoke(client, "flags", "list")
        assert "persist_me" in flags_result.output

    def test_invalid_type_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client, "flags", "create", "bad_flag", "--name", "Bad", "--type", "invalid_type"
        )
        assert result.exit_code != 0

    def test_create_duplicate_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "create", "my_flag", "--name", "Dupe")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags enable / disable
# ---------------------------------------------------------------------------


class TestFlagsEnableDisable:
    def test_enable_flag(self):
        engine = _seed_engine_with_flags(_make_flag(enabled=False))
        client = _open_client(engine)
        result = invoke(client, "flags", "enable", "my_flag")
        assert result.exit_code == 0
        assert "enabled" in result.output.lower()

    def test_disable_flag(self):
        engine = _seed_engine_with_flags(_make_flag(enabled=True))
        client = _open_client(engine)
        result = invoke(client, "flags", "disable", "my_flag")
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_enable_missing_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "enable", "nonexistent")
        assert result.exit_code != 0

    def test_disable_missing_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "disable", "nonexistent")
        assert result.exit_code != 0

    def test_enable_then_verify_via_list(self):
        engine = _seed_engine_with_flags(_make_flag(enabled=False))
        client = _open_client(engine)
        invoke(client, "flags", "enable", "my_flag")
        result = invoke(client, "flags", "list", "--status", "enabled")
        assert "my_flag" in result.output


# ---------------------------------------------------------------------------
# waygate flags delete
# ---------------------------------------------------------------------------


class TestFlagsDelete:
    def test_delete_with_yes_flag(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "delete", "my_flag", "--yes")
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_delete_removes_from_list(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        invoke(client, "flags", "delete", "my_flag", "--yes")
        result = invoke(client, "flags", "list")
        assert "my_flag" not in result.output

    def test_delete_missing_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "delete", "nonexistent", "--yes")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags eval
# ---------------------------------------------------------------------------


class TestFlagsEval:
    def test_eval_basic(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "my_flag", "--key", "user_1")
        assert result.exit_code == 0
        assert "value" in result.output
        assert "reason" in result.output

    def test_eval_shows_value(self):
        engine = _seed_engine_with_flags(_make_flag())  # fallthrough="on"
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "my_flag", "--key", "user_1")
        assert result.exit_code == 0
        assert "True" in result.output

    def test_eval_with_attributes(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "eval",
            "my_flag",
            "--key",
            "user_1",
            "--attr",
            "role=admin",
            "--attr",
            "plan=pro",
        )
        assert result.exit_code == 0

    def test_eval_invalid_attr_format(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "my_flag", "--attr", "not_key_value")
        assert result.exit_code != 0

    def test_eval_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "nonexistent", "--key", "user_1")
        assert result.exit_code != 0

    def test_eval_disabled_shows_off_value(self):
        engine = _seed_engine_with_flags(_make_flag(enabled=False))
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "my_flag", "--key", "user_1")
        assert result.exit_code == 0
        # Disabled flag → off variation (False)
        assert "False" in result.output

    def test_eval_shows_reason(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "eval", "my_flag", "--key", "user_1")
        assert result.exit_code == 0
        assert "FALLTHROUGH" in result.output or "OFF" in result.output or "reason" in result.output


# ---------------------------------------------------------------------------
# waygate segments list
# ---------------------------------------------------------------------------


class TestSegmentsList:
    def test_empty_shows_message(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "segments", "list")
        assert result.exit_code == 0
        assert "No segments" in result.output

    def test_shows_segment_row(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(client, "segments", "list")
        assert result.exit_code == 0
        assert "beta" in result.output

    def test_shows_count(self):
        engine = _seed_engine_with_flags(segments=[_make_segment("a"), _make_segment("b")])
        client = _open_client(engine)
        result = invoke(client, "segments", "list")
        assert result.exit_code == 0
        assert "2 segment" in result.output


# ---------------------------------------------------------------------------
# waygate segments get
# ---------------------------------------------------------------------------


class TestSegmentsGet:
    def test_shows_segment_details(self):
        engine = _seed_engine_with_flags(segments=[_make_segment(included=["u1", "u2"])])
        client = _open_client(engine)
        result = invoke(client, "segments", "get", "beta")
        assert result.exit_code == 0
        assert "beta" in result.output

    def test_shows_included_members(self):
        engine = _seed_engine_with_flags(segments=[_make_segment(included=["user_1"])])
        client = _open_client(engine)
        result = invoke(client, "segments", "get", "beta")
        assert result.exit_code == 0
        assert "user_1" in result.output

    def test_missing_segment_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "segments", "get", "nonexistent")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate segments create
# ---------------------------------------------------------------------------


class TestSegmentsCreate:
    def test_create_segment(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "segments", "create", "beta", "--name", "Beta Users")
        assert result.exit_code == 0
        assert "beta" in result.output
        assert "created" in result.output.lower()

    def test_create_persists_segment(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        invoke(client, "segments", "create", "pro", "--name", "Pro Users")
        result = invoke(client, "segments", "list")
        assert "pro" in result.output

    def test_create_duplicate_exits_nonzero(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(client, "segments", "create", "beta", "--name", "Beta")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate segments delete
# ---------------------------------------------------------------------------


class TestSegmentsDelete:
    def test_delete_with_yes(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(client, "segments", "delete", "beta", "--yes")
        assert result.exit_code == 0
        assert "deleted" in result.output.lower()

    def test_delete_removes_from_list(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        invoke(client, "segments", "delete", "beta", "--yes")
        result = invoke(client, "segments", "list")
        assert "beta" not in result.output

    def test_delete_missing_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "segments", "delete", "nonexistent", "--yes")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate segments include / exclude
# ---------------------------------------------------------------------------


class TestSegmentsIncludeExclude:
    def test_include_adds_keys(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(
            client,
            "segments",
            "include",
            "beta",
            "--context-key",
            "user_1,user_2",
        )
        assert result.exit_code == 0
        assert "2" in result.output  # 2 keys added

        # Verify via get
        get_result = invoke(client, "segments", "get", "beta")
        assert "user_1" in get_result.output

    def test_include_deduplicates(self):
        engine = _seed_engine_with_flags(segments=[_make_segment(included=["user_1"])])
        client = _open_client(engine)
        result = invoke(
            client,
            "segments",
            "include",
            "beta",
            "--context-key",
            "user_1,user_2",
        )
        assert result.exit_code == 0
        # Only user_2 is new
        assert "1" in result.output

    def test_exclude_adds_keys(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(
            client,
            "segments",
            "exclude",
            "beta",
            "--context-key",
            "user_99",
        )
        assert result.exit_code == 0
        assert "1" in result.output

    def test_include_missing_segment_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client,
            "segments",
            "include",
            "nonexistent",
            "--context-key",
            "user_1",
        )
        assert result.exit_code != 0

    def test_exclude_missing_segment_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client,
            "segments",
            "exclude",
            "nonexistent",
            "--context-key",
            "user_1",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate seg alias
# ---------------------------------------------------------------------------


class TestSegAlias:
    def test_seg_alias_works(self):
        engine = _seed_engine_with_flags(segments=[_make_segment()])
        client = _open_client(engine)
        result = invoke(client, "seg", "list")
        assert result.exit_code == 0
        assert "beta" in result.output


# ---------------------------------------------------------------------------
# waygate flags edit  (PATCH / LaunchDarkly-style mutation)
# ---------------------------------------------------------------------------


class TestFlagsEdit:
    def test_edit_name(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "edit", "my_flag", "--name", "Renamed")
        assert result.exit_code == 0

    def test_edit_name_persists(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        invoke(client, "flags", "edit", "my_flag", "--name", "Renamed")
        flag = anyio.run(engine.get_flag, "my_flag")
        assert flag.name == "Renamed"

    def test_edit_description(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "edit", "my_flag", "--description", "A test flag")
        assert result.exit_code == 0

    def test_edit_off_variation(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "edit", "my_flag", "--off-variation", "on")
        assert result.exit_code == 0
        flag = anyio.run(engine.get_flag, "my_flag")
        assert flag.off_variation == "on"

    def test_edit_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "edit", "no_such_flag", "--name", "x")
        assert result.exit_code != 0

    def test_edit_no_options_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "edit", "my_flag")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags variations  (read-only list)
# ---------------------------------------------------------------------------


class TestFlagsVariations:
    def test_shows_variation_names(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "variations", "my_flag")
        assert result.exit_code == 0
        assert "on" in result.output
        assert "off" in result.output

    def test_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "variations", "no_such_flag")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags targeting  (read-only view)
# ---------------------------------------------------------------------------


class TestFlagsTargeting:
    def test_shows_off_variation(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "targeting", "my_flag")
        assert result.exit_code == 0
        assert "off" in result.output  # off_variation value

    def test_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "targeting", "no_such_flag")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags add-rule / remove-rule
# ---------------------------------------------------------------------------


class TestFlagsAddRule:
    def test_add_segment_rule(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "add-rule",
            "my_flag",
            "--variation",
            "on",
            "--segment",
            "beta-users",
        )
        assert result.exit_code == 0
        flag = anyio.run(engine.get_flag, "my_flag")
        assert len(flag.rules) == 1
        assert flag.rules[0].clauses[0].operator == "in_segment"
        assert flag.rules[0].clauses[0].values == ["beta-users"]
        assert flag.rules[0].variation == "on"

    def test_add_attribute_rule(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "add-rule",
            "my_flag",
            "--variation",
            "on",
            "--attribute",
            "plan",
            "--operator",
            "is",
            "--values",
            "pro,enterprise",
        )
        assert result.exit_code == 0
        flag = anyio.run(engine.get_flag, "my_flag")
        assert len(flag.rules) == 1
        assert flag.rules[0].clauses[0].attribute == "plan"
        assert flag.rules[0].clauses[0].values == ["pro", "enterprise"]

    def test_add_rule_output_shows_rule_id(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "add-rule",
            "my_flag",
            "--variation",
            "on",
            "--segment",
            "vip",
        )
        assert result.exit_code == 0
        assert "id:" in result.output

    def test_add_rule_no_segment_or_attribute_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "add-rule", "my_flag", "--variation", "on")
        assert result.exit_code != 0

    def test_add_rule_both_segment_and_attribute_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "add-rule",
            "my_flag",
            "--variation",
            "on",
            "--segment",
            "beta",
            "--attribute",
            "plan",
        )
        assert result.exit_code != 0

    def test_add_rule_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "add-rule",
            "no_such_flag",
            "--variation",
            "on",
            "--segment",
            "beta",
        )
        assert result.exit_code != 0


class TestFlagsRemoveRule:
    def _flag_with_rule(self) -> tuple[FeatureFlag, str]:
        from waygate.core.feature_flags.models import Operator, RuleClause, TargetingRule

        rule = TargetingRule(
            clauses=[RuleClause(attribute="key", operator=Operator.IN_SEGMENT, values=["beta"])],
            variation="on",
        )
        flag = _make_flag()
        flag.rules = [rule]
        return flag, rule.id

    def test_remove_rule_by_id(self):
        flag, rule_id = self._flag_with_rule()
        engine = _seed_engine_with_flags(flag)
        client = _open_client(engine)
        result = invoke(client, "flags", "remove-rule", "my_flag", "--rule-id", rule_id)
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "my_flag")
        assert updated.rules == []

    def test_remove_rule_unknown_id_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client,
            "flags",
            "remove-rule",
            "my_flag",
            "--rule-id",
            "00000000-0000-0000-0000-000000000000",
        )
        assert result.exit_code != 0

    def test_remove_rule_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "remove-rule", "no_such_flag", "--rule-id", "abc")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags add-prereq / remove-prereq
# ---------------------------------------------------------------------------


def _make_two_flags() -> tuple[FeatureFlag, FeatureFlag]:
    return _make_flag("flag_a"), _make_flag("flag_b")


class TestFlagsAddPrereq:
    def test_add_prereq(self):
        flag_a, flag_b = _make_two_flags()
        engine = _seed_engine_with_flags(flag_a, flag_b)
        client = _open_client(engine)
        result = invoke(
            client, "flags", "add-prereq", "flag_a", "--flag", "flag_b", "--variation", "on"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "flag_a")
        assert len(updated.prerequisites) == 1
        assert updated.prerequisites[0].flag_key == "flag_b"
        assert updated.prerequisites[0].variation == "on"

    def test_add_prereq_updates_existing(self):
        flag_a, flag_b = _make_two_flags()
        engine = _seed_engine_with_flags(flag_a, flag_b)
        client = _open_client(engine)
        invoke(client, "flags", "add-prereq", "flag_a", "--flag", "flag_b", "--variation", "on")
        result = invoke(
            client, "flags", "add-prereq", "flag_a", "--flag", "flag_b", "--variation", "off"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "flag_a")
        assert len(updated.prerequisites) == 1
        assert updated.prerequisites[0].variation == "off"

    def test_add_prereq_self_reference_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "add-prereq", "my_flag", "--flag", "my_flag", "--variation", "on"
        )
        assert result.exit_code != 0

    def test_add_prereq_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client, "flags", "add-prereq", "no_such_flag", "--flag", "other", "--variation", "on"
        )
        assert result.exit_code != 0


class TestFlagsRemovePrereq:
    def _flag_with_prereq(self) -> FeatureFlag:
        from waygate.core.feature_flags.models import Prerequisite

        flag = _make_flag("flag_a")
        flag.prerequisites = [Prerequisite(flag_key="flag_b", variation="on")]
        return flag

    def test_remove_prereq(self):
        flag = self._flag_with_prereq()
        engine = _seed_engine_with_flags(flag)
        client = _open_client(engine)
        result = invoke(client, "flags", "remove-prereq", "flag_a", "--flag", "flag_b")
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "flag_a")
        assert updated.prerequisites == []

    def test_remove_prereq_not_found_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(client, "flags", "remove-prereq", "my_flag", "--flag", "nonexistent_prereq")
        assert result.exit_code != 0

    def test_remove_prereq_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(client, "flags", "remove-prereq", "no_such_flag", "--flag", "other")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# waygate flags target / untarget
# ---------------------------------------------------------------------------


class TestFlagsTarget:
    def test_target_pins_keys(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "target", "my_flag", "--variation", "on", "--keys", "user_1,user_2"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "my_flag")
        assert "user_1" in updated.targets.get("on", [])
        assert "user_2" in updated.targets.get("on", [])

    def test_target_appends_without_duplicates(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        invoke(client, "flags", "target", "my_flag", "--variation", "on", "--keys", "user_1")
        result = invoke(
            client, "flags", "target", "my_flag", "--variation", "on", "--keys", "user_1,user_2"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "my_flag")
        assert updated.targets["on"].count("user_1") == 1
        assert "user_2" in updated.targets["on"]

    def test_target_unknown_variation_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "target", "my_flag", "--variation", "unknown", "--keys", "user_1"
        )
        assert result.exit_code != 0

    def test_target_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client, "flags", "target", "no_such_flag", "--variation", "on", "--keys", "user_1"
        )
        assert result.exit_code != 0


class TestFlagsUntarget:
    def _flag_with_targets(self) -> FeatureFlag:
        flag = _make_flag()
        flag.targets = {"on": ["user_1", "user_2"]}
        return flag

    def test_untarget_removes_keys(self):
        engine = _seed_engine_with_flags(self._flag_with_targets())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "untarget", "my_flag", "--variation", "on", "--keys", "user_1"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "my_flag")
        assert "user_1" not in updated.targets.get("on", [])
        assert "user_2" in updated.targets.get("on", [])

    def test_untarget_removes_variation_when_empty(self):
        engine = _seed_engine_with_flags(self._flag_with_targets())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "untarget", "my_flag", "--variation", "on", "--keys", "user_1,user_2"
        )
        assert result.exit_code == 0
        updated = anyio.run(engine.get_flag, "my_flag")
        assert "on" not in updated.targets

    def test_untarget_no_targets_exits_nonzero(self):
        engine = _seed_engine_with_flags(_make_flag())
        client = _open_client(engine)
        result = invoke(
            client, "flags", "untarget", "my_flag", "--variation", "on", "--keys", "user_1"
        )
        assert result.exit_code != 0

    def test_untarget_missing_flag_exits_nonzero(self):
        engine = WaygateEngine()
        client = _open_client(engine)
        result = invoke(
            client, "flags", "untarget", "no_such_flag", "--variation", "on", "--keys", "user_1"
        )
        assert result.exit_code != 0
