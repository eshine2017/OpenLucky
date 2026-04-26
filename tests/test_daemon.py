"""Tests for app.daemon — Job lifecycle orchestration."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from app.daemon import Daemon
from app.models import ChatState, ChatStatus, JobStatus, RunResult, SessionDecision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_result(
    *,
    exit_code: int = 0,
    summary: str = "All done",
    session_id: str = "new-session-id",
    stdout: str = "output",
    stderr: str = "",
) -> RunResult:
    return RunResult(
        session_id=session_id,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        summary=summary,
    )


def _make_daemon(tmp_path) -> tuple[Daemon, MagicMock, MagicMock, MagicMock, MagicMock]:
    """
    Return (daemon, mock_db, mock_agent, mock_session_manager, mock_send).
    Jobs dir is inside tmp_path so log files land somewhere real.
    """
    jobs_dir = str(tmp_path / "jobs")
    mock_db = MagicMock()
    mock_agent = MagicMock()
    mock_agent.name = "claude"
    mock_session_manager = MagicMock()
    mock_send = MagicMock()

    daemon = Daemon(
        db_module=mock_db,
        agent=mock_agent,
        session_manager=mock_session_manager,
        send_message_fn=mock_send,
        jobs_dir=jobs_dir,
        default_cwd="/tmp/openlucky_work",
    )
    return daemon, mock_db, mock_agent, mock_session_manager, mock_send


def _default_chat_state(chat_id: str = "42") -> ChatState:
    return ChatState(
        telegram_chat_id=chat_id,
        active_session_id="prev-session",
        cwd="/tmp",
        status=ChatStatus.idle,
        active_task_name="my-task",
    )


def _default_decision(mode: str = "resume") -> SessionDecision:
    return SessionDecision(mode=mode, session_id="session-abc")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_initial_state(self, tmp_path):
        daemon, _, _, _, _ = _make_daemon(tmp_path)
        assert daemon.running_locks == {}
        assert isinstance(daemon._lock, type(threading.Lock()))

    def test_stores_dependencies(self, tmp_path):
        daemon, mock_db, mock_agent, mock_sm, mock_send = _make_daemon(tmp_path)
        assert daemon._db is mock_db
        assert daemon._agent is mock_agent
        assert daemon._session_manager is mock_sm
        assert daemon._send is mock_send
        assert daemon._default_cwd == "/tmp/openlucky_work"


# ---------------------------------------------------------------------------
# on_message() — busy guard
# ---------------------------------------------------------------------------

class TestOnMessageBusyGuard:
    def test_rejects_when_job_already_running(self, tmp_path):
        daemon, _, _, _, mock_send = _make_daemon(tmp_path)
        daemon.running_locks["42"] = "existing-job-id"

        daemon.on_message("42", "do something")

        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        assert args[0] == "42"
        assert "already running" in args[1].lower() or "running" in args[1].lower()

    def test_does_not_create_job_when_busy(self, tmp_path):
        daemon, mock_db, _, _, _ = _make_daemon(tmp_path)
        daemon.running_locks["42"] = "existing-job-id"

        daemon.on_message("42", "do something")

        mock_db.create_job.assert_not_called()


# ---------------------------------------------------------------------------
# on_message() — job creation
# ---------------------------------------------------------------------------

class TestOnMessageJobCreation:
    def _setup(self, tmp_path, mode="resume", force_new=False, has_chat=True):
        daemon, mock_db, mock_agent, mock_sm, mock_send = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()

        chat_state = _default_chat_state()
        chat_state.force_new_next = force_new
        mock_db.get_chat.return_value = chat_state if has_chat else None
        mock_sm.decide.return_value = _default_decision(mode=mode)

        return daemon, mock_db, mock_agent, mock_sm, mock_send

    def test_creates_job_record(self, tmp_path):
        daemon, mock_db, _, _, _ = self._setup(tmp_path)
        daemon.on_message("42", "hello")
        # Wait for background thread
        time.sleep(0.2)
        mock_db.create_job.assert_called_once()
        job = mock_db.create_job.call_args[0][0]
        assert job.telegram_chat_id == "42"
        assert job.user_message == "hello"
        assert job.status == JobStatus.queued

    def test_creates_new_chat_state_when_none(self, tmp_path):
        daemon, mock_db, _, _, _ = self._setup(tmp_path, has_chat=False)
        daemon.on_message("42", "hello")
        time.sleep(0.2)
        # session_manager.decide should still be called with a fresh ChatState
        call_args = daemon._session_manager.decide.call_args
        chat_state_arg = call_args[0][0]
        assert chat_state_arg.telegram_chat_id == "42"

    def test_force_new_consumed_and_saved(self, tmp_path):
        daemon, mock_db, _, _, _ = self._setup(tmp_path, force_new=True)
        daemon.on_message("42", "hello")
        time.sleep(0.2)
        # upsert_chat should have been called to consume force_new_next
        # (at least once before the job runs)
        assert mock_db.upsert_chat.called

    def test_lock_set_during_execution(self, tmp_path):
        """running_locks must be set before the thread is launched."""
        daemon, mock_db, mock_agent, _, _ = self._setup(tmp_path)

        # Make agent.run take a moment so we can observe the lock
        started = threading.Event()
        def slow_run(*args, **kwargs):
            started.set()
            time.sleep(0.05)
            return _make_run_result()

        mock_agent.run.side_effect = slow_run

        daemon.on_message("42", "hello")
        started.wait(timeout=1.0)
        assert "42" in daemon.running_locks

        # Let it finish
        time.sleep(0.15)

    def test_lock_released_after_execution(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = self._setup(tmp_path)
        daemon.on_message("42", "hello")
        time.sleep(0.3)
        assert "42" not in daemon.running_locks


# ---------------------------------------------------------------------------
# _run_job() — success path
# ---------------------------------------------------------------------------

class TestRunJobSuccess:
    def test_agent_run_called_with_correct_args(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "do the thing")
        time.sleep(0.3)

        mock_agent.run.assert_called_once()
        call_kw = mock_agent.run.call_args[1]
        assert call_kw["prompt"] == "do the thing"
        assert call_kw["cwd"] == "/tmp"
        assert call_kw["session_id"] == "session-abc"

    def test_sends_start_and_running_messages(self, tmp_path):
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        # At least 3 send calls: start, running, result
        assert mock_send.call_count >= 3

    def test_job_marked_done_on_success(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(exit_code=0)
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        update_calls = mock_db.update_job.call_args_list
        final_job = update_calls[-1][0][0]
        assert final_job.status == JobStatus.done

    def test_chat_state_updated_to_idle_on_success(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(exit_code=0, session_id="new-sid")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        upsert_calls = mock_db.upsert_chat.call_args_list
        final_chat = upsert_calls[-1][0][0]
        assert final_chat.status == ChatStatus.idle
        assert final_chat.active_session_id == "new-sid"

    def test_raw_output_written_to_log(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(stdout="job output here")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        job_logs = list((tmp_path / "jobs").glob("*.log"))
        assert len(job_logs) == 1
        content = job_logs[0].read_text(encoding="utf-8")
        assert "job output here" in content

    def test_stderr_appended_to_log(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(stdout="out", stderr="err msg")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        job_logs = list((tmp_path / "jobs").glob("*.log"))
        content = job_logs[0].read_text(encoding="utf-8")
        assert "err msg" in content
        assert "STDERR" in content


# ---------------------------------------------------------------------------
# _run_job() — failure path (non-zero exit code)
# ---------------------------------------------------------------------------

class TestRunJobFailure:
    def test_job_marked_failed_on_nonzero_exit(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(exit_code=1, stderr="oops")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        update_calls = mock_db.update_job.call_args_list
        final_job = update_calls[-1][0][0]
        assert final_job.status == JobStatus.failed
        assert final_job.exit_code == 1

    def test_chat_state_updated_to_error_on_failure(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(exit_code=1)
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        upsert_calls = mock_db.upsert_chat.call_args_list
        final_chat = upsert_calls[-1][0][0]
        assert final_chat.status == ChatStatus.error

    def test_error_message_sent_to_telegram(self, tmp_path):
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result(exit_code=1, stderr="bad error")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        all_messages = [str(c[0][1]) for c in mock_send.call_args_list]
        # At least one message should mention failure
        combined = " ".join(all_messages)
        assert any(kw in combined.lower() for kw in ["error", "failed", "exit"])


# ---------------------------------------------------------------------------
# _run_job() — session archiving on "new" mode
# ---------------------------------------------------------------------------

class TestSessionArchiving:
    def test_archives_old_session_on_new_mode(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()

        chat_state = _default_chat_state()
        chat_state.active_session_id = "old-session"
        mock_db.get_chat.return_value = chat_state
        daemon._session_manager.decide.return_value = SessionDecision(
            mode="new", session_id="new-session-xyz"
        )

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        mock_db.archive_session.assert_called_once_with(
            "old-session", "42", "my-task", "/tmp"
        )

    def test_no_archive_on_resume_mode(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision(mode="resume")

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        mock_db.archive_session.assert_not_called()


# ---------------------------------------------------------------------------
# _run_job() — exception handling
# ---------------------------------------------------------------------------

class TestRunJobException:
    def test_exception_marks_job_failed(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.side_effect = RuntimeError("agent crashed")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        update_calls = mock_db.update_job.call_args_list
        final_job = update_calls[-1][0][0]
        assert final_job.status == JobStatus.failed
        assert final_job.exit_code == -1

    def test_exception_sends_error_to_telegram(self, tmp_path):
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        mock_agent.run.side_effect = ValueError("something broke")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        all_messages = [str(c[0][1]) for c in mock_send.call_args_list]
        combined = " ".join(all_messages)
        assert "something broke" in combined

    def test_lock_released_even_on_exception(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.side_effect = RuntimeError("crash")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        assert "42" not in daemon.running_locks

    def test_exception_updates_chat_state_to_error(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.side_effect = RuntimeError("crash")
        mock_db.get_chat.return_value = _default_chat_state()
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        upsert_calls = mock_db.upsert_chat.call_args_list
        final_chat = upsert_calls[-1][0][0]
        assert final_chat.status == ChatStatus.error


# ---------------------------------------------------------------------------
# default_cwd fallback
# ---------------------------------------------------------------------------

class TestDefaultCwd:
    def test_uses_default_cwd_when_chat_has_none(self, tmp_path):
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_agent.run.return_value = _make_run_result()

        chat_state = _default_chat_state()
        chat_state.cwd = None
        mock_db.get_chat.return_value = chat_state
        daemon._session_manager.decide.return_value = _default_decision()

        daemon.on_message("42", "hello")
        time.sleep(0.3)

        call_kw = mock_agent.run.call_args[1]
        assert call_kw["cwd"] == "/tmp/openlucky_work"
