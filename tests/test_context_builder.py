"""Tests for app.context_builder."""

from __future__ import annotations

import inspect
import os

import pytest

from app.context_builder import (
    _MAX_SECTION_CHARS,
    _MAX_TOTAL_CHARS,
    ContextBuilder,
    read_user_timezone,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_builder(tmp_path, *, templates_dir: str | None = None) -> ContextBuilder:
    """Create a ContextBuilder with workspace at tmp_path/workspace."""
    workspace = str(tmp_path / "workspace")
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    builder = ContextBuilder(workspace_dir=workspace)
    if templates_dir is not None:
        # Patch _load_template to use a custom dir so templates don't interfere
        object.__setattr__(builder, "_soul_template", "")
        object.__setattr__(builder, "_user_template", "")
        object.__setattr__(builder, "_memory_template", "")
    return builder


def _make_builder_no_templates(tmp_path) -> ContextBuilder:
    return _make_builder(tmp_path, templates_dir="")


# ---------------------------------------------------------------------------
# build_prefix
# ---------------------------------------------------------------------------


def test_build_prefix_with_all_files(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    _write(os.path.join(workspace, "SOUL.md"), "I am a bot.")
    _write(os.path.join(workspace, "USER.md"), "Name: Alice")
    _write(os.path.join(workspace, "memory", "MEMORY.md"), "Remember: Python 3.12")

    prefix = builder.build_prefix()

    assert "bot_identity" in prefix
    assert "I am a bot." in prefix
    assert "user_profile" in prefix
    assert "Name: Alice" in prefix
    assert "user_memory" in prefix
    assert "Remember: Python 3.12" in prefix
    assert 'trust="user-controlled"' in prefix


def test_build_prefix_missing_files(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    # No files created — should return "" without raising
    prefix = builder.build_prefix()
    assert prefix == ""


def test_build_prefix_empty_file(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir
    _write(os.path.join(workspace, "SOUL.md"), "")
    _write(os.path.join(workspace, "USER.md"), "Name: Bob")
    _write(os.path.join(workspace, "memory", "MEMORY.md"), "")

    prefix = builder.build_prefix()

    assert "bot_identity" not in prefix
    assert "Name: Bob" in prefix
    assert "user_memory" not in prefix


def test_build_prefix_template_content_skipped(tmp_path):
    builder = _make_builder(tmp_path)  # keeps real templates
    workspace = builder._workspace_dir

    # Write the same content as the SOUL template
    template_content = builder._soul_template
    if not template_content:
        pytest.skip("No SOUL template found on disk")

    _write(os.path.join(workspace, "SOUL.md"), template_content)

    prefix = builder.build_prefix()
    assert "bot_identity" not in prefix


def test_build_prefix_caps_section_at_max_chars(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    big_content = "x" * (_MAX_SECTION_CHARS + 500)
    _write(os.path.join(workspace, "SOUL.md"), big_content)

    prefix = builder.build_prefix()
    assert "truncated" in prefix
    # Section content is capped; allow overhead for XML tags + footer
    assert len(prefix) < _MAX_SECTION_CHARS + 600


def test_build_prefix_caps_total(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    big = "y" * (_MAX_SECTION_CHARS - 10)
    _write(os.path.join(workspace, "SOUL.md"), big)
    _write(os.path.join(workspace, "USER.md"), big)
    _write(os.path.join(workspace, "memory", "MEMORY.md"), big)

    prefix = builder.build_prefix()
    # Total must not vastly exceed cap (allow overhead for XML tags + footer)
    assert len(prefix) <= _MAX_TOTAL_CHARS + 500


def test_build_prefix_returns_empty_string_when_all_blank(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    prefix = builder.build_prefix()
    assert prefix == ""
    assert isinstance(prefix, str)


def test_build_prefix_io_error_skips_section(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    _write(os.path.join(workspace, "SOUL.md"), "good content")
    _write(os.path.join(workspace, "USER.md"), "Name: Dave")

    # Make SOUL.md unreadable
    os.chmod(os.path.join(workspace, "SOUL.md"), 0o000)
    try:
        prefix = builder.build_prefix()
    finally:
        os.chmod(os.path.join(workspace, "SOUL.md"), 0o644)

    # Should not raise; should still include USER.md
    assert "Name: Dave" in prefix
    assert "bot_identity" not in prefix


def test_build_prefix_invalid_utf8(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    soul_path = os.path.join(workspace, "SOUL.md")
    os.makedirs(os.path.dirname(soul_path), exist_ok=True)
    with open(soul_path, "wb") as fh:
        fh.write(b"Hello \xff\xfe world")

    # Should not crash
    prefix = builder.build_prefix()
    assert isinstance(prefix, str)


def test_build_prefix_oversized_file(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    soul_path = os.path.join(workspace, "SOUL.md")
    os.makedirs(os.path.dirname(soul_path), exist_ok=True)
    # Write > 256 KB
    with open(soul_path, "w", encoding="utf-8") as fh:
        fh.write("a" * (300 * 1024))

    prefix = builder.build_prefix()
    # Should truncate, not OOM
    assert isinstance(prefix, str)
    assert len(prefix) < 400 * 1024


# ---------------------------------------------------------------------------
# read_soul / read_user / read_memory — hardcoded, no path parameter
# ---------------------------------------------------------------------------


def test_read_soul_user_memory_hardcoded(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir

    _write(os.path.join(workspace, "SOUL.md"), "soul content")
    _write(os.path.join(workspace, "USER.md"), "user content")
    _write(os.path.join(workspace, "memory", "MEMORY.md"), "memory content")

    assert builder.read_soul() == "soul content"
    assert builder.read_user() == "user content"
    assert builder.read_memory() == "memory content"

    # These methods accept no path parameter — verify the API shape
    for method_name in ("read_soul", "read_user", "read_memory"):
        sig = inspect.signature(getattr(builder, method_name))
        params = [p for p in sig.parameters if p != "self"]
        assert params == [], f"{method_name} should take no parameters, got {params}"


# ---------------------------------------------------------------------------
# build_resume_hint
# ---------------------------------------------------------------------------


def test_build_resume_hint_contains_path(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    hint = builder.build_resume_hint()
    assert builder._workspace_dir in hint
    assert "\n" not in hint.strip()  # should be a single line


# ---------------------------------------------------------------------------
# second_brain_dir
# ---------------------------------------------------------------------------


def test_build_prefix_footer_includes_second_brain_path(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    second_brain = "/home/user/vault"
    builder = ContextBuilder(workspace_dir=workspace, second_brain_dir=second_brain)
    object.__setattr__(builder, "_soul_template", "")
    object.__setattr__(builder, "_user_template", "")
    object.__setattr__(builder, "_memory_template", "")

    _write(os.path.join(workspace, "SOUL.md"), "I am a bot.")

    prefix = builder.build_prefix()
    assert second_brain in prefix


def test_build_prefix_second_brain_when_no_sections(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    second_brain = "/home/user/vault"
    builder = ContextBuilder(workspace_dir=workspace, second_brain_dir=second_brain)
    object.__setattr__(builder, "_soul_template", "")
    object.__setattr__(builder, "_user_template", "")
    object.__setattr__(builder, "_memory_template", "")

    # No workspace files — sections will be empty, but prefix still carries the full footer
    prefix = builder.build_prefix()
    assert second_brain in prefix
    assert "Memory files live at:" in prefix  # full footer, not a bare one-liner


def test_build_prefix_no_second_brain_dir_unchanged(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    workspace = builder._workspace_dir
    _write(os.path.join(workspace, "SOUL.md"), "I am a bot.")

    prefix = builder.build_prefix()
    assert "Second brain" not in prefix


def test_build_resume_hint_includes_second_brain_path(tmp_path):
    workspace = str(tmp_path / "workspace")
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    second_brain = "/home/user/vault"
    builder = ContextBuilder(workspace_dir=workspace, second_brain_dir=second_brain)

    hint = builder.build_resume_hint()
    assert second_brain in hint
    assert "\n" not in hint.strip()  # must remain single-line


def test_build_resume_hint_without_second_brain_unchanged(tmp_path):
    builder = _make_builder_no_templates(tmp_path)
    hint = builder.build_resume_hint()
    assert "Second brain" not in hint


# ---------------------------------------------------------------------------
# read_user_timezone
# ---------------------------------------------------------------------------


class TestReadUserTimezone:
    def _write_user_md(self, tmp_path, content: str) -> str:
        path = str(tmp_path / "USER.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return str(tmp_path)

    def test_plain_format(self, tmp_path) -> None:
        ws = self._write_user_md(tmp_path, "timezone: America/Los_Angeles\n")
        assert read_user_timezone(ws) == "America/Los_Angeles"

    def test_list_item_format(self, tmp_path) -> None:
        ws = self._write_user_md(
            tmp_path, "- **Timezone**: America/Los_Angeles (UTC-7/8, Bay Area)\n"
        )
        assert read_user_timezone(ws) == "America/Los_Angeles"

    def test_markdown_bold_with_utc_annotation(self, tmp_path) -> None:
        ws = self._write_user_md(tmp_path, "**Timezone**: America/Los_Angeles (UTC-8/UTC-7)\n")
        assert read_user_timezone(ws) == "America/Los_Angeles"

    def test_markdown_bold_no_annotation(self, tmp_path) -> None:
        ws = self._write_user_md(tmp_path, "**Timezone**: Europe/London\n")
        assert read_user_timezone(ws) == "Europe/London"

    def test_case_insensitive(self, tmp_path) -> None:
        ws = self._write_user_md(tmp_path, "TIMEZONE: UTC\n")
        assert read_user_timezone(ws) == "UTC"

    def test_invalid_iana_name_returns_none(self, tmp_path) -> None:
        ws = self._write_user_md(tmp_path, "timezone: Not/AZone\n")
        assert read_user_timezone(ws) is None

    def test_no_user_md_returns_none(self, tmp_path) -> None:
        assert read_user_timezone(str(tmp_path)) is None
