"""Tests for app.command_help — command metadata and help text renderer."""

from app.command_help import (
    COMMANDS,
    TOP_LEVEL_NAMES,
    _CATEGORY_LABELS,
    _CATEGORY_ORDER,
    _SCHEDULE_SUBCMDS,
    render_help,
)


class TestCommandSpecs:
    def test_all_names_start_with_bang(self) -> None:
        for spec in COMMANDS:
            assert spec.name.startswith("!"), f"{spec.name!r} does not start with '!'"

    def test_all_names_unique(self) -> None:
        names = [spec.name for spec in COMMANDS]
        assert len(names) == len(set(names))

    def test_all_categories_in_category_order(self) -> None:
        for spec in COMMANDS:
            assert spec.category in _CATEGORY_ORDER, (
                f"{spec.name!r} has category {spec.category!r} not in _CATEGORY_ORDER"
            )


class TestTopLevelNames:
    def test_matches_names_in_commands(self) -> None:
        expected = frozenset(spec.name for spec in COMMANDS)
        assert TOP_LEVEL_NAMES == expected

    def test_is_frozenset(self) -> None:
        assert isinstance(TOP_LEVEL_NAMES, frozenset)


class TestRenderHelp:
    def test_contains_available_commands_header(self) -> None:
        assert "Available commands:" in render_help()

    def test_no_unknown_prefix_by_default(self) -> None:
        assert "Unknown command" not in render_help()

    def test_unknown_prefix_with_none(self) -> None:
        # None is the default; no prefix added
        result = render_help(unknown=None)
        assert "Unknown command" not in result

    def test_unknown_command_prefix_shown(self) -> None:
        result = render_help(unknown="!bogus")
        assert "Unknown command: !bogus" in result

    def test_unknown_prefix_followed_by_header(self) -> None:
        result = render_help(unknown="!bogus")
        assert result.index("Unknown command: !bogus") < result.index("Available commands:")

    def test_contains_all_category_labels(self) -> None:
        result = render_help()
        for label in _CATEGORY_LABELS.values():
            assert label in result, f"Category label {label!r} missing from help output"

    def test_category_labels_wrapped_in_brackets(self) -> None:
        result = render_help()
        for label in _CATEGORY_LABELS.values():
            assert f"[{label}]" in result

    def test_categories_appear_in_order(self) -> None:
        result = render_help()
        positions = []
        for cat in _CATEGORY_ORDER:
            label = _CATEGORY_LABELS[cat]
            pos = result.find(f"[{label}]")
            assert pos != -1, f"[{label}] not found in output"
            positions.append(pos)
        assert positions == sorted(positions)

    def test_contains_non_schedule_command_usages(self) -> None:
        result = render_help()
        for spec in COMMANDS:
            if spec.category == "schedule":
                continue
            assert spec.usage in result, f"Usage {spec.usage!r} missing from help"

    def test_contains_non_schedule_command_summaries(self) -> None:
        result = render_help()
        for spec in COMMANDS:
            if spec.category == "schedule":
                continue
            assert spec.summary in result, f"Summary {spec.summary!r} missing from help"

    def test_schedule_section_uses_subcmds_not_parent(self) -> None:
        result = render_help()
        # Parent entry "!schedule <subcmd>" should not appear; sub-commands should
        assert "!schedule <subcmd>" not in result
        for sub in _SCHEDULE_SUBCMDS:
            assert sub.usage in result, f"Sub-command usage {sub.usage!r} missing"

    def test_schedule_subcmd_summaries_present(self) -> None:
        result = render_help()
        for sub in _SCHEDULE_SUBCMDS:
            assert sub.summary in result, f"Sub-command summary {sub.summary!r} missing"
