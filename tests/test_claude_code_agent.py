"""Tests for app.agents.claude_code — command building and output parsing."""

import json
import signal
from unittest.mock import MagicMock, patch

from app.agents.claude_code import ClaudeCodeAgent


def _make_runner() -> ClaudeCodeAgent:
    return ClaudeCodeAgent(claude_bin="claude", work_dir="/tmp/test_work")


class TestBuildCommand:
    def test_without_session(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hello world", session_id=None)
        assert cmd == [
            "claude",
            "-p",
            "hello world",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def test_with_session(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hello", session_id="sess-123")
        assert cmd == [
            "claude",
            "-p",
            "hello",
            "--output-format",
            "stream-json",
            "--verbose",
            "--resume",
            "sess-123",
        ]

    def test_empty_session_id_not_added(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hi", session_id="")
        assert "--resume" not in cmd


class TestParseStreamJson:
    def test_valid_result_event(self) -> None:
        runner = _make_runner()
        output = json.dumps(
            {
                "type": "result",
                "session_id": "abc-123",
                "result": "All done!",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "abc-123"
        assert summary == "All done!"

    def test_no_result_event_fallback_to_assistant(self) -> None:
        runner = _make_runner()
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Working on it..."}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Here is the answer."}],
                    },
                }
            ),
        ]
        output = "\n".join(lines)
        session_id, summary = runner._parse_stream_json(output)
        assert session_id is None
        # Falls back to last assistant text
        assert summary == "Here is the answer."

    def test_empty_output(self) -> None:
        runner = _make_runner()
        session_id, summary = runner._parse_stream_json("")
        assert session_id is None
        assert summary == "(No summary available)"

    def test_invalid_json_lines_skipped(self) -> None:
        runner = _make_runner()
        output = "not json\n{bad json too\n" + json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": "ok",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "s1"
        assert summary == "ok"

    def test_long_summary_truncated(self) -> None:
        runner = _make_runner()
        long_text = "x" * 4000
        output = json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": long_text,
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert len(summary) <= 3020  # 3000 + len("… (truncated)") + newline
        assert "truncated" in summary

    def test_result_without_session_id(self) -> None:
        runner = _make_runner()
        output = json.dumps(
            {
                "type": "result",
                "result": "Some output",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id is None
        assert summary == "Some output"

    def test_mixed_events(self) -> None:
        runner = _make_runner()
        lines = [
            json.dumps({"type": "system", "data": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "thinking..."}]},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "s99",
                    "result": "Final answer",
                }
            ),
        ]
        output = "\n".join(lines)
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "s99"
        assert summary == "Final answer"


# ---------------------------------------------------------------------------
# run() — subprocess integration (mocked Popen)
# ---------------------------------------------------------------------------

def _make_popen_mock(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


class TestRun:
    @patch("subprocess.Popen")
    def test_run_returns_run_result(self, mock_popen, tmp_path):
        result_line = json.dumps({"type": "result", "session_id": "s1", "result": "Done"})
        mock_popen.return_value = _make_popen_mock(stdout=result_line + "\n", returncode=0)

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        result = runner.run(prompt="hello", cwd=str(tmp_path), session_id="s1", job_id="j1")

        assert result.exit_code == 0
        assert result.session_id == "s1"
        assert result.summary == "Done"
        assert result.stdout == result_line + "\n"

    @patch("subprocess.Popen")
    def test_run_with_no_session_id(self, mock_popen, tmp_path):
        mock_popen.return_value = _make_popen_mock(returncode=0)

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        result = runner.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id=None)

        assert result.exit_code == 0
        # No session_id in stdout → falls back to empty string
        assert result.session_id == ""

    @patch("subprocess.Popen")
    def test_run_falls_back_to_work_dir_when_cwd_missing(self, mock_popen, tmp_path):
        mock_popen.return_value = _make_popen_mock()

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        # Non-existent cwd → should fall back to work_dir
        runner.run(prompt="hi", cwd="/nonexistent/path", session_id=None, job_id="jx")

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)

    @patch("subprocess.Popen")
    def test_run_uses_provided_cwd_when_it_exists(self, mock_popen, tmp_path):
        mock_popen.return_value = _make_popen_mock()

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir="/tmp")
        runner.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id="jy")

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)

    @patch("subprocess.Popen")
    def test_run_registers_and_deregisters_process(self, mock_popen, tmp_path):
        proc = _make_popen_mock()
        mock_popen.return_value = proc

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        runner.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id="j-reg")

        # After run completes, process should be deregistered
        assert "j-reg" not in runner._processes

    @patch("subprocess.Popen")
    def test_run_nonzero_exit_code_propagated(self, mock_popen, tmp_path):
        mock_popen.return_value = _make_popen_mock(returncode=2, stderr="bad error")

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        result = runner.run(prompt="hi", cwd=str(tmp_path), session_id=None, job_id="j-fail")

        assert result.exit_code == 2
        assert result.stderr == "bad error"

    @patch("subprocess.Popen")
    def test_run_preserves_session_id_when_not_in_output(self, mock_popen, tmp_path):
        """If stdout has no session_id, fall back to the passed-in session_id."""
        mock_popen.return_value = _make_popen_mock(stdout="", returncode=0)

        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        result = runner.run(prompt="hi", cwd=str(tmp_path), session_id="fallback-sid", job_id="jf")

        assert result.session_id == "fallback-sid"


# ---------------------------------------------------------------------------
# cancel() — SIGTERM + SIGKILL fallback
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_no_process_is_noop(self, tmp_path):
        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        # Should not raise
        runner.cancel("nonexistent-job")

    def test_cancel_sends_sigterm(self, tmp_path):
        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        proc.poll.return_value = 0  # already exited after SIGTERM

        runner._processes["j-term"] = proc

        runner.cancel("j-term")

        proc.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_cancel_sends_sigkill_if_process_does_not_stop(self, tmp_path):
        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        # poll() always returns None → process never exits → SIGKILL should fire
        proc.poll.return_value = None

        runner._processes["j-kill"] = proc

        # Patch time.monotonic to fast-forward past the 5-second deadline
        import time as _time
        call_count = [0]
        start = _time.monotonic()

        def _fast_monotonic():
            call_count[0] += 1
            # First call returns start, subsequent calls return well past deadline
            if call_count[0] == 1:
                return start
            return start + 10.0  # past the 5-second deadline

        with (
            patch("app.agents.claude_code.time.monotonic", side_effect=_fast_monotonic),
            patch("app.agents.claude_code.time.sleep"),
        ):
            runner.cancel("j-kill")

        # Should have sent SIGTERM then SIGKILL
        calls = proc.send_signal.call_args_list
        signals_sent = [c[0][0] for c in calls]
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent

    def test_cancel_handles_process_lookup_error_on_sigterm(self, tmp_path):
        """If the process is already gone when SIGTERM is sent, should return silently."""
        runner = ClaudeCodeAgent(claude_bin="claude", work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        proc.send_signal.side_effect = ProcessLookupError

        runner._processes["j-gone"] = proc

        # Should not raise
        runner.cancel("j-gone")
