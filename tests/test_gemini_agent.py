"""Tests for app.agents.gemini_code — GeminiAgent command building and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agents.gemini_code import GeminiAgent


def _make_agent(**kwargs: str) -> GeminiAgent:
    defaults: dict = {"gemini_bin": "gemini", "work_dir": "/tmp/test_work"}
    defaults.update(kwargs)
    return GeminiAgent(**defaults)


def _make_popen_mock(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


class TestBuildCommand:
    def test_always_includes_skip_trust(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id=None)
        assert "--skip-trust" in cmd

    def test_always_includes_approval_mode_auto_edit(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id=None)
        assert "--approval-mode" in cmd
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == "auto_edit"

    def test_always_includes_json_output_format(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id=None)
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    def test_no_session_mints_uuid_for_session_id_flag(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id=None)
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        val = cmd[idx + 1]
        parsed = uuid.UUID(val)  # raises if not valid UUID
        assert str(parsed) == val

    def test_no_session_does_not_include_resume(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id=None)
        assert "--resume" not in cmd

    def test_truthy_session_uses_resume_flag(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id="my-session-id")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "my-session-id"

    def test_truthy_session_does_not_include_session_id_flag(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hello", session_id="my-session-id")
        assert "--session-id" not in cmd

    def test_empty_session_id_treated_as_new(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hi", session_id="")
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    def test_prompt_as_last_two_args(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("my prompt", session_id=None)
        assert cmd[-2] == "-p"
        assert cmd[-1] == "my prompt"

    def test_no_model_flag_when_not_configured(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hi", session_id=None)
        assert "-m" not in cmd

    def test_model_flag_when_configured(self) -> None:
        agent = _make_agent(gemini_model="gemini-2.0-flash")
        cmd = agent._build_command("hi", session_id=None)
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-2.0-flash"

    def test_per_call_model_overrides_ctor_default(self) -> None:
        agent = _make_agent(gemini_model="gemini-2.0-flash")
        cmd = agent._build_command("hi", session_id=None, model="gemini-2.5-pro")
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-2.5-pro"
        assert cmd.count("-m") == 1

    def test_per_call_model_without_ctor_default(self) -> None:
        agent = _make_agent()
        cmd = agent._build_command("hi", session_id=None, model="gemini-2.5-pro")
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-2.5-pro"

    def test_existing_workspace_dir_added(self, tmp_path) -> None:
        ws = str(tmp_path / "ws")
        os.makedirs(ws)
        agent = _make_agent(workspace_dir=ws)
        cmd = agent._build_command("hi", session_id=None)
        assert "--include-directories" in cmd

    def test_nonexistent_second_brain_dir_skipped(self) -> None:
        agent = _make_agent(second_brain_dir="/nonexistent/vault")
        cmd = agent._build_command("hi", session_id=None)
        assert "/nonexistent/vault" not in " ".join(cmd)

    def test_multiple_existing_dirs_joined_comma(self, tmp_path) -> None:
        ws = str(tmp_path / "ws")
        sb = str(tmp_path / "sb")
        os.makedirs(ws)
        os.makedirs(sb)
        agent = _make_agent(workspace_dir=ws, second_brain_dir=sb)
        cmd = agent._build_command("hi", session_id=None)
        assert "--include-directories" in cmd
        idx = cmd.index("--include-directories")
        dirs_val = cmd[idx + 1]
        assert ws in dirs_val
        assert sb in dirs_val
        assert "," in dirs_val

    def test_images_dir_included_when_image_paths_present(self, tmp_path) -> None:
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        agent = _make_agent(images_dir=images_dir)
        cmd = agent._build_command("hi", session_id=None, image_paths=["/data/a.jpg"])
        assert "--include-directories" in cmd
        idx = cmd.index("--include-directories")
        assert images_dir in cmd[idx + 1]

    def test_images_dir_not_included_without_image_paths(self, tmp_path) -> None:
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        agent = _make_agent(images_dir=images_dir)
        cmd = agent._build_command("hi", session_id=None, image_paths=[])
        if "--include-directories" in cmd:
            idx = cmd.index("--include-directories")
            assert images_dir not in cmd[idx + 1]

    def test_no_duplicate_include_directories_flag(self, tmp_path) -> None:
        ws = str(tmp_path / "ws")
        os.makedirs(ws)
        agent = _make_agent(workspace_dir=ws)
        cmd = agent._build_command("hi", session_id=None)
        assert cmd.count("--include-directories") == 1


class TestParseOutput:
    def test_valid_json_extracts_response_and_session_id(self) -> None:
        agent = _make_agent()
        payload = json.dumps({"session_id": "sid-1", "response": "Done!", "stats": {}})
        session_id, summary = agent._parse_output(payload, "sid-1")
        assert session_id == "sid-1"
        assert summary == "Done!"

    def test_fallback_on_invalid_json(self) -> None:
        agent = _make_agent()
        session_id, summary = agent._parse_output("not valid json", "fallback-sid")
        assert session_id == "fallback-sid"
        assert "not valid json" in summary

    def test_long_response_truncated(self) -> None:
        agent = _make_agent()
        long_text = "x" * 4000
        payload = json.dumps({"session_id": "s1", "response": long_text})
        _, summary = agent._parse_output(payload, "s1")
        assert len(summary) <= 3020
        assert "truncated" in summary

    def test_json_session_id_takes_precedence_over_fallback(self) -> None:
        agent = _make_agent()
        payload = json.dumps({"session_id": "json-sid", "response": "ok"})
        session_id, _ = agent._parse_output(payload, "passed-sid")
        assert session_id == "json-sid"

    def test_missing_session_id_in_json_falls_back(self) -> None:
        agent = _make_agent()
        payload = json.dumps({"response": "ok"})
        session_id, _ = agent._parse_output(payload, "fallback-sid")
        assert session_id == "fallback-sid"

    def test_empty_stdout_falls_back(self) -> None:
        agent = _make_agent()
        session_id, summary = agent._parse_output("", "fb-sid")
        assert session_id == "fb-sid"


class TestRun:
    @patch("subprocess.Popen")
    def test_new_session_returns_minted_id(self, mock_popen, tmp_path) -> None:
        payload = json.dumps({"session_id": "minted-123", "response": "Done"})
        mock_popen.return_value = _make_popen_mock(stdout=payload, returncode=0)

        agent = _make_agent(work_dir=str(tmp_path))
        result = agent.run(prompt="hello", cwd=str(tmp_path), session_id=None, job_id="j1")

        assert result.exit_code == 0
        assert result.session_id == "minted-123"
        assert result.summary == "Done"

    @patch("subprocess.Popen")
    def test_resume_session_passes_resume_flag(self, mock_popen, tmp_path) -> None:
        payload = json.dumps({"session_id": "sid-existing", "response": "Resumed"})
        mock_popen.return_value = _make_popen_mock(stdout=payload, returncode=0)

        agent = _make_agent(work_dir=str(tmp_path))
        result = agent.run(prompt="hi", cwd=str(tmp_path), session_id="sid-existing", job_id="j2")

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        assert "sid-existing" in cmd
        assert result.session_id == "sid-existing"

    @patch("subprocess.Popen")
    def test_skip_trust_always_present_in_command(self, mock_popen, tmp_path) -> None:
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"response": "ok"}), returncode=0
        )

        agent = _make_agent(work_dir=str(tmp_path))
        agent.run(prompt="test", cwd=str(tmp_path))

        cmd = mock_popen.call_args[0][0]
        assert "--skip-trust" in cmd

    @patch("subprocess.Popen")
    def test_fallback_session_id_when_json_parse_fails(self, mock_popen, tmp_path) -> None:
        mock_popen.return_value = _make_popen_mock(stdout="not json", returncode=0)

        agent = _make_agent(work_dir=str(tmp_path))
        result = agent.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id="j3")

        # session_id should be the minted UUID (non-empty string)
        assert result.session_id
        # Should be a valid UUID
        uuid.UUID(result.session_id)

    @patch("subprocess.Popen")
    def test_nonzero_exit_code_propagated(self, mock_popen, tmp_path) -> None:
        mock_popen.return_value = _make_popen_mock(stderr="bad error", returncode=42)

        agent = _make_agent(work_dir=str(tmp_path))
        result = agent.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id="j4")

        assert result.exit_code == 42

    @patch("subprocess.Popen")
    def test_run_passes_model_to_command(self, mock_popen, tmp_path) -> None:
        mock_popen.return_value = _make_popen_mock(
            stdout=json.dumps({"response": "ok"}), returncode=0
        )

        agent = _make_agent(work_dir=str(tmp_path))
        agent.run(
            prompt="hi", cwd=str(tmp_path), session_id=None, job_id="j5", model="gemini-2.5-pro"
        )

        cmd = mock_popen.call_args[0][0]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "gemini-2.5-pro"


class TestRealBinary:
    """Integration test — skipped when gemini binary is absent."""

    def test_real_binary_headless(self, tmp_path) -> None:
        if not shutil.which("gemini"):
            pytest.skip("gemini binary not found")

        agent = _make_agent(work_dir=str(tmp_path))
        result = agent.run(prompt="say: hello", cwd=str(tmp_path))
        assert result.exit_code == 0
        assert result.summary
