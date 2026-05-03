"""Tests for app.digest — Morning digest prompt builder."""

from __future__ import annotations

import os
import time

from app.digest import build_morning_digest_prompt, read_user_timezone

# ---------------------------------------------------------------------------
# build_morning_digest_prompt
# ---------------------------------------------------------------------------


class TestBuildMorningDigestPrompt:
    def test_empty_dir_returns_none(self) -> None:
        assert build_morning_digest_prompt("") is None

    def test_nonexistent_dir_returns_none(self, tmp_path) -> None:
        missing = str(tmp_path / "no_such_dir")
        assert build_morning_digest_prompt(missing) is None

    def test_now_md_content_in_prompt(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "now.md").write_text("- Buy groceries\n- Ship feature", encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        assert prompt is not None
        assert "Buy groceries" in prompt

    def test_prompt_includes_instructions(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "now.md").write_text("- task", encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        assert prompt is not None
        lower = prompt.lower()
        assert "digest" in lower or "summarize" in lower or "morning" in lower

    def test_project_readmes_included(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "now.md").write_text("- todo", encoding="utf-8")

        projects = tmp_path / "projects"
        projects.mkdir()
        proj_a = projects / "alpha"
        proj_a.mkdir()
        (proj_a / "README.md").write_text("# Alpha\nProject alpha description", encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        assert prompt is not None
        assert "alpha" in prompt.lower() or "Project alpha" in prompt

    def test_projects_ordered_by_mtime(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "now.md").write_text("- todo", encoding="utf-8")

        projects = tmp_path / "projects"
        projects.mkdir()

        # Create proj_old first
        old = projects / "old_proj"
        old.mkdir()
        (old / "README.md").write_text("# Old project", encoding="utf-8")

        # Set mtime to old
        old_time = time.time() - 10000
        os.utime(old / "README.md", (old_time, old_time))

        # Create proj_new later (newer mtime)
        new = projects / "new_proj"
        new.mkdir()
        (new / "README.md").write_text("# New project", encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        assert prompt is not None
        # New project should appear before old project
        assert prompt.index("New project") < prompt.index("Old project")

    def test_now_md_capped_at_16kb(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        large_content = "x" * 100_000  # 100 KB
        (inbox / "now.md").write_text(large_content, encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        assert prompt is not None
        # The large content should be truncated
        assert len(prompt) < len(large_content) + 500

    def test_missing_now_md_still_works(self, tmp_path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # No now.md created

        projects = tmp_path / "projects"
        projects.mkdir()
        (projects / "myproj").mkdir()
        (projects / "myproj" / "README.md").write_text("# My project", encoding="utf-8")

        prompt = build_morning_digest_prompt(str(tmp_path))

        # Should still return a prompt with whatever is available
        assert prompt is not None

    def test_no_sources_at_all_returns_none(self, tmp_path) -> None:
        # Empty dir structure
        (tmp_path / "inbox").mkdir()
        (tmp_path / "projects").mkdir()

        result = build_morning_digest_prompt(str(tmp_path))

        assert result is None


# ---------------------------------------------------------------------------
# read_user_timezone
# ---------------------------------------------------------------------------


class TestReadUserTimezone:
    def test_valid_tz_in_user_md(self, tmp_path) -> None:
        (tmp_path / "USER.md").write_text(
            "# User\ntimezone: America/Los_Angeles\nsome other line\n",
            encoding="utf-8",
        )
        result = read_user_timezone(str(tmp_path))
        assert result == "America/Los_Angeles"

    def test_case_insensitive_key(self, tmp_path) -> None:
        (tmp_path / "USER.md").write_text(
            "Timezone: Europe/London\n",
            encoding="utf-8",
        )
        result = read_user_timezone(str(tmp_path))
        assert result == "Europe/London"

    def test_invalid_tz_returns_none(self, tmp_path) -> None:
        (tmp_path / "USER.md").write_text(
            "timezone: Not/AReal/Zone\n",
            encoding="utf-8",
        )
        result = read_user_timezone(str(tmp_path))
        assert result is None

    def test_missing_user_md_returns_none(self, tmp_path) -> None:
        result = read_user_timezone(str(tmp_path))
        assert result is None

    def test_no_timezone_line_returns_none(self, tmp_path) -> None:
        (tmp_path / "USER.md").write_text(
            "# User profile\nname: Alice\nrole: engineer\n",
            encoding="utf-8",
        )
        result = read_user_timezone(str(tmp_path))
        assert result is None
