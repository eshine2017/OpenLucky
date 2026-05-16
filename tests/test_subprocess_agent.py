"""Tests for app.agents.subprocess_agent — shared subprocess base."""

from __future__ import annotations

import signal
import time
from unittest.mock import MagicMock, patch

from app.agents.subprocess_agent import SubprocessAgent
from app.models import RunResult


class _ConcreteAgent(SubprocessAgent):
    """Minimal concrete subclass for testing the abstract base."""

    name = "test"

    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        image_paths: list[str] | None = None,
    ) -> list[str]:
        return ["echo", prompt]

    def run(
        self,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        job_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> RunResult:
        cmd = self._build_command(prompt, session_id, image_paths)
        stdout, stderr, exit_code = self._spawn(cmd, cwd, job_id)
        return RunResult(
            session_id=session_id or "",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            summary=self._truncate(stdout.strip()),
        )


class TestCancel:
    def test_cancel_no_process_is_noop(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        agent.cancel("nonexistent-job")  # should not raise

    def test_cancel_sends_sigterm(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        proc.poll.return_value = 0  # exits after SIGTERM

        agent._processes["j-term"] = proc
        agent.cancel("j-term")

        proc.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_cancel_sends_sigkill_if_process_does_not_stop(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        proc.poll.return_value = None  # never exits

        agent._processes["j-kill"] = proc

        call_count = [0]
        start = time.monotonic()

        def _fast_monotonic() -> float:
            call_count[0] += 1
            if call_count[0] == 1:
                return start
            return start + 10.0  # past 5-second deadline

        with (
            patch("app.agents.subprocess_agent.time.monotonic", side_effect=_fast_monotonic),
            patch("app.agents.subprocess_agent.time.sleep"),
        ):
            agent.cancel("j-kill")

        calls = proc.send_signal.call_args_list
        signals_sent = [c[0][0] for c in calls]
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent

    def test_cancel_handles_process_lookup_error_on_sigterm(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        proc = MagicMock()
        proc.pid = 9999
        proc.send_signal.side_effect = ProcessLookupError

        agent._processes["j-gone"] = proc
        agent.cancel("j-gone")  # should not raise


class TestTruncate:
    def test_short_text_unchanged(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        assert agent._truncate("hello") == "hello"

    def test_long_text_truncated_at_3000(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        long = "x" * 4000
        result = agent._truncate(long)
        assert len(result) <= 3020
        assert "truncated" in result

    def test_empty_text_unchanged(self, tmp_path) -> None:
        agent = _ConcreteAgent(work_dir=str(tmp_path))
        assert agent._truncate("") == ""


class TestSpawn:
    @patch("subprocess.Popen")
    def test_spawn_returns_stdout_stderr_exitcode(self, mock_popen, tmp_path) -> None:
        proc = MagicMock()
        proc.communicate.return_value = ("out", "err")
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        agent = _ConcreteAgent(work_dir=str(tmp_path))
        stdout, stderr, code = agent._spawn(["echo", "hi"], str(tmp_path), "j1")

        assert stdout == "out"
        assert stderr == "err"
        assert code == 0

    @patch("subprocess.Popen")
    def test_spawn_registers_and_deregisters_process(self, mock_popen, tmp_path) -> None:
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        agent = _ConcreteAgent(work_dir=str(tmp_path))
        agent._spawn(["echo", "hi"], str(tmp_path), "j-reg")

        assert "j-reg" not in agent._processes

    @patch("subprocess.Popen")
    def test_spawn_falls_back_to_work_dir_when_cwd_missing(self, mock_popen, tmp_path) -> None:
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        agent = _ConcreteAgent(work_dir=str(tmp_path))
        agent._spawn(["echo"], "/nonexistent/path", "jx")

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)
