"""Unit tests for app.bootstrap — state detection, file state, completion signal."""

from __future__ import annotations

import pytest

from app.bootstrap import COMPLETION_SENTINEL, BootstrapChecker, BootstrapState, is_complete_signal
from app.models import ChatState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "memory").mkdir()
    return ws


@pytest.fixture()
def templates_dir(tmp_path):
    td = tmp_path / "templates"
    td.mkdir()
    (td / "SOUL.md").write_text("# Bot Identity\n\nTemplate content soul.", encoding="utf-8")
    (td / "USER.md").write_text("# User Profile\n\nTemplate content user.", encoding="utf-8")
    (td / "BOOTSTRAP.md").write_text(
        "# Setup\nWorkspace: {workspace_dir}\n[[BOOTSTRAP_COMPLETE]]",
        encoding="utf-8",
    )
    return td


@pytest.fixture()
def checker(workspace, templates_dir):
    return BootstrapChecker(
        workspace_dir=str(workspace),
        templates_dir=str(templates_dir),
    )


def _chat(bootstrap_session_id: str | None = None) -> ChatState:
    return ChatState(
        telegram_chat_id="42",
        bootstrap_session_id=bootstrap_session_id,
    )


# ---------------------------------------------------------------------------
# State: NEEDED
# ---------------------------------------------------------------------------


class TestStateNeeded:
    @pytest.mark.unit
    def test_needed_when_soul_missing(self, checker, workspace):
        (workspace / "USER.md").write_text("filled user content", encoding="utf-8")
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.NEEDED
        assert bs.soul == "missing"

    @pytest.mark.unit
    def test_needed_when_user_md_missing(self, checker, workspace):
        (workspace / "SOUL.md").write_text("filled soul content", encoding="utf-8")
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.NEEDED
        assert bs.user == "missing"

    @pytest.mark.unit
    def test_needed_when_soul_at_template(self, checker, workspace):
        (workspace / "SOUL.md").write_text(
            "# Bot Identity\n\nTemplate content soul.", encoding="utf-8"
        )
        (workspace / "USER.md").write_text("filled user content", encoding="utf-8")
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.NEEDED
        assert bs.soul == "template"

    @pytest.mark.unit
    def test_needed_when_user_md_at_template(self, checker, workspace):
        (workspace / "SOUL.md").write_text("filled soul content", encoding="utf-8")
        (workspace / "USER.md").write_text(
            "# User Profile\n\nTemplate content user.", encoding="utf-8"
        )
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.NEEDED
        assert bs.user == "template"


# ---------------------------------------------------------------------------
# State: IN_PROGRESS
# ---------------------------------------------------------------------------


class TestStateInProgress:
    @pytest.mark.unit
    def test_in_progress_when_session_id_set_and_files_unfilled(self, checker):
        bs = checker.check(_chat(bootstrap_session_id="sess-123"))
        assert bs.state == BootstrapState.IN_PROGRESS
        assert bs.session_id == "sess-123"

    @pytest.mark.unit
    def test_in_progress_session_id_propagated(self, checker):
        bs = checker.check(_chat(bootstrap_session_id="abc-xyz"))
        assert bs.session_id == "abc-xyz"


# ---------------------------------------------------------------------------
# State: COMPLETE
# ---------------------------------------------------------------------------


class TestStateComplete:
    @pytest.mark.unit
    def test_complete_when_soul_and_user_filled(self, checker, workspace):
        (workspace / "SOUL.md").write_text("filled soul", encoding="utf-8")
        (workspace / "USER.md").write_text("filled user", encoding="utf-8")
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.COMPLETE
        assert bs.soul == "filled"
        assert bs.user == "filled"

    @pytest.mark.unit
    def test_complete_when_memory_still_empty(self, checker, workspace):
        (workspace / "SOUL.md").write_text("filled soul", encoding="utf-8")
        (workspace / "USER.md").write_text("filled user", encoding="utf-8")
        # memory/MEMORY.md deliberately absent — must not block completion
        bs = checker.check(_chat())
        assert bs.state == BootstrapState.COMPLETE

    @pytest.mark.unit
    def test_complete_session_id_is_none(self, checker, workspace):
        (workspace / "SOUL.md").write_text("filled soul", encoding="utf-8")
        (workspace / "USER.md").write_text("filled user", encoding="utf-8")
        bs = checker.check(_chat(bootstrap_session_id="stale-id"))
        assert bs.state == BootstrapState.COMPLETE
        assert bs.session_id is None


# ---------------------------------------------------------------------------
# _file_state
# ---------------------------------------------------------------------------


class TestFileState:
    @pytest.mark.unit
    def test_missing_when_file_absent(self, checker):
        assert checker._file_state("SOUL.md", "template") == "missing"

    @pytest.mark.unit
    def test_missing_on_empty_file(self, checker, workspace):
        (workspace / "SOUL.md").write_text("", encoding="utf-8")
        assert checker._file_state("SOUL.md", "template") == "missing"

    @pytest.mark.unit
    def test_template_when_content_matches(self, checker, workspace):
        template = "# Bot Identity\n\nTemplate content soul."
        (workspace / "SOUL.md").write_text(template, encoding="utf-8")
        assert checker._file_state("SOUL.md", template) == "template"

    @pytest.mark.unit
    def test_filled_when_content_differs(self, checker, workspace):
        template = "# Bot Identity\n\nTemplate content soul."
        (workspace / "SOUL.md").write_text("I am a custom bot.", encoding="utf-8")
        assert checker._file_state("SOUL.md", template) == "filled"

    @pytest.mark.unit
    def test_missing_on_oserror(self, checker, tmp_path):
        # Checker whose workspace_dir points at a non-existent directory
        bad_checker = BootstrapChecker(
            workspace_dir=str(tmp_path / "nonexistent"),
            templates_dir=str(tmp_path),
        )
        assert bad_checker._file_state("SOUL.md", "template") == "missing"

    @pytest.mark.unit
    def test_template_comparison_is_strip_insensitive(self, checker, workspace):
        template = "# User Profile\n\nTemplate content user."
        (workspace / "USER.md").write_text(
            "\n# User Profile\n\nTemplate content user.\n\n", encoding="utf-8"
        )
        assert checker._file_state("USER.md", template) == "template"

    @pytest.mark.unit
    def test_raises_on_disallowed_filename(self, checker):
        with pytest.raises(ValueError, match="only accepts"):
            checker._file_state("../../etc/passwd", "template")


# ---------------------------------------------------------------------------
# Completion signal
# ---------------------------------------------------------------------------


class TestIsCompleteSignal:
    @pytest.mark.unit
    def test_detects_sentinel_in_stdout(self):
        assert is_complete_signal(f"done\n{COMPLETION_SENTINEL}\nbye", "") is True

    @pytest.mark.unit
    def test_detects_sentinel_in_summary(self):
        assert is_complete_signal("", f"Setup complete. {COMPLETION_SENTINEL}") is True

    @pytest.mark.unit
    def test_false_when_absent(self):
        assert is_complete_signal("some output", "some summary") is False

    @pytest.mark.unit
    def test_false_on_empty_strings(self):
        assert is_complete_signal("", "") is False

    @pytest.mark.unit
    def test_detects_sentinel_embedded_in_longer_text(self):
        assert is_complete_signal(f"prefix{COMPLETION_SENTINEL}suffix", "") is True


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


class TestLoadBootstrapPrompt:
    @pytest.mark.unit
    def test_returns_content(self, checker):
        prompt = checker.load_bootstrap_prompt()
        assert "Setup" in prompt
        assert len(prompt) > 10

    @pytest.mark.unit
    def test_substitutes_workspace_dir(self, checker, workspace):
        prompt = checker.load_bootstrap_prompt()
        assert str(workspace) in prompt
        assert "{workspace_dir}" not in prompt

    @pytest.mark.unit
    def test_fallback_when_bootstrap_md_missing(self, workspace, tmp_path):
        empty_templates = tmp_path / "empty_templates"
        empty_templates.mkdir()
        c = BootstrapChecker(str(workspace), str(empty_templates))
        prompt = c.load_bootstrap_prompt()
        assert len(prompt) > 10  # fallback is non-empty
        assert "{workspace_dir}" not in prompt

    @pytest.mark.unit
    def test_no_format_error_when_path_has_braces(self, workspace, tmp_path):
        braces_dir = tmp_path / "work{x}"
        braces_dir.mkdir()
        td = tmp_path / "tpl"
        td.mkdir()
        (td / "BOOTSTRAP.md").write_text("path: {workspace_dir}", encoding="utf-8")
        c = BootstrapChecker(str(braces_dir), str(td))
        # Must not raise KeyError
        prompt = c.load_bootstrap_prompt()
        assert str(braces_dir) in prompt
