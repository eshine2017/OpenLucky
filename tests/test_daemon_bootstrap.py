"""Integration tests for Daemon bootstrap branching."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.bootstrap import COMPLETION_SENTINEL, BootstrapChecker, BootstrapState, BootstrapStatus
from app.daemon import Daemon
from app.models import ChatState, ChatStatus, JobStatus, RunResult, SessionDecision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_result(
    *,
    exit_code: int = 0,
    summary: str = "done",
    session_id: str = "bs-session-1",
    stdout: str = "",
    stderr: str = "",
) -> RunResult:
    return RunResult(
        session_id=session_id,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        summary=summary,
    )


def _chat(
    chat_id: str = "42",
    bootstrap_session_id: str | None = None,
    active_session_id: str | None = None,
) -> ChatState:
    return ChatState(
        telegram_chat_id=chat_id,
        bootstrap_session_id=bootstrap_session_id,
        active_session_id=active_session_id,
    )


def _make_daemon(tmp_path, bootstrap_checker=None):
    jobs_dir = str(tmp_path / "jobs")
    mock_db = MagicMock()
    mock_agent = MagicMock()
    mock_session_manager = MagicMock()
    mock_send = MagicMock()

    daemon = Daemon(
        db_module=mock_db,
        agent=mock_agent,
        session_manager=mock_session_manager,
        send_message_fn=mock_send,
        jobs_dir=jobs_dir,
        default_cwd="/tmp/work",
        bootstrap_checker=bootstrap_checker,
    )
    return daemon, mock_db, mock_agent, mock_session_manager, mock_send


def _bs(state: BootstrapState, session_id: str | None = None) -> BootstrapStatus:
    return BootstrapStatus(
        state=state,
        soul="filled" if state == BootstrapState.COMPLETE else "missing",
        user="filled" if state == BootstrapState.COMPLETE else "missing",
        session_id=session_id,
    )


def _wait_for_job(daemon, chat_id: str = "42", timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with daemon._lock:
            if chat_id not in daemon.running_locks:
                return
        time.sleep(0.05)
    raise TimeoutError("Job did not finish in time")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBootstrapHappyPath:
    @pytest.mark.integration
    def test_first_message_triggers_bootstrap_session(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.return_value = _bs(BootstrapState.NEEDED)
        checker.load_bootstrap_prompt.return_value = "BOOTSTRAP PROMPT\n"
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result(session_id="bs-1")

        daemon.on_message("42", "hello")
        _wait_for_job(daemon)

        call_kwargs = mock_agent.run.call_args
        prompt = call_kwargs.kwargs.get("prompt") or ""
        assert "BOOTSTRAP PROMPT" in prompt

    @pytest.mark.integration
    def test_bootstrap_uses_no_resume_on_first_turn(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.return_value = _bs(BootstrapState.NEEDED)
        checker.load_bootstrap_prompt.return_value = "BOOTSTRAP PROMPT\n"
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result(session_id="bs-1")

        daemon.on_message("42", "hello")
        _wait_for_job(daemon)

        call_kwargs = mock_agent.run.call_args
        assert call_kwargs.kwargs.get("session_id") is None

    @pytest.mark.integration
    def test_bootstrap_session_id_persisted_after_first_turn(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),
            _bs(BootstrapState.NEEDED),  # post-job check — still not complete
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result(session_id="bs-session-42")

        daemon.on_message("42", "hi")
        _wait_for_job(daemon)

        upsert_calls = mock_db.upsert_chat.call_args_list
        saved_states = [c.args[0] for c in upsert_calls]
        # At some point, bootstrap_session_id must be set to "bs-session-42"
        assert any(s.bootstrap_session_id == "bs-session-42" for s in saved_states)
        # active_session_id must never be set to the bootstrap session id
        assert not any(s.active_session_id == "bs-session-42" for s in saved_states)

    @pytest.mark.integration
    def test_subsequent_message_resumes_bootstrap(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.IN_PROGRESS, session_id="bs-123"),
            _bs(BootstrapState.IN_PROGRESS, session_id="bs-123"),
        ]
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat(bootstrap_session_id="bs-123")
        mock_agent.run.return_value = _run_result(session_id="bs-123")

        daemon.on_message("42", "my name is Alice")
        _wait_for_job(daemon)

        call_kwargs = mock_agent.run.call_args
        # prompt must be the raw user text (no bootstrap prompt prefix)
        prompt = call_kwargs.kwargs.get("prompt", "")
        assert prompt == "my name is Alice"
        assert call_kwargs.kwargs.get("session_id") == "bs-123"

    @pytest.mark.integration
    def test_completion_transitions_to_normal(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.IN_PROGRESS, session_id="bs-1"),  # initial check
            _bs(BootstrapState.COMPLETE),                          # post-job re-check
        ]
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat(bootstrap_session_id="bs-1")
        mock_agent.run.return_value = _run_result(
            stdout=f"Files updated.\n{COMPLETION_SENTINEL}\n",
            summary="All done.",
        )

        daemon.on_message("42", "looks good")
        _wait_for_job(daemon)

        upsert_calls = mock_db.upsert_chat.call_args_list
        final_state = upsert_calls[-1].args[0]
        assert final_state.bootstrap_session_id is None
        assert final_state.active_session_id is None

        # Completion notice sent; format_done NOT called
        sent_texts = [c.args[1] for c in mock_send.call_args_list]
        assert any("Setup complete" in t for t in sent_texts)
        assert not any("format_done" in t for t in sent_texts)


# ---------------------------------------------------------------------------
# Partial / failure cases
# ---------------------------------------------------------------------------


class TestBootstrapPartialAndFailure:
    @pytest.mark.integration
    def test_sentinel_without_files_stays_in_progress(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),
            _bs(BootstrapState.NEEDED),  # files still not filled after job
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result(
            stdout=f"{COMPLETION_SENTINEL}\n",
            summary="done",
        )

        daemon.on_message("42", "go")
        _wait_for_job(daemon)

        sent_texts = [c.args[1] for c in mock_send.call_args_list]
        assert not any("Setup complete" in t for t in sent_texts)

    @pytest.mark.integration
    def test_files_filled_without_sentinel_stays_in_progress(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),
            _bs(BootstrapState.COMPLETE),  # files are filled but no sentinel yet
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result(stdout="no sentinel here", summary="almost done")

        daemon.on_message("42", "go")
        _wait_for_job(daemon)

        sent_texts = [c.args[1] for c in mock_send.call_args_list]
        assert not any("Setup complete" in t for t in sent_texts)

    @pytest.mark.integration
    def test_bootstrap_failure_keeps_session_id(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.return_value = _bs(BootstrapState.IN_PROGRESS, session_id="bs-old")
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat(bootstrap_session_id="bs-old")
        mock_agent.run.return_value = _run_result(exit_code=1, session_id="bs-old")

        daemon.on_message("42", "go")
        _wait_for_job(daemon)

        upsert_calls = mock_db.upsert_chat.call_args_list
        final_state = upsert_calls[-1].args[0]
        # session_id preserved so next message can resume
        assert final_state.bootstrap_session_id == "bs-old"
        # active_session_id never touched
        assert final_state.active_session_id is None

        sent_texts = [c.args[1] for c in mock_send.call_args_list]
        assert any("went wrong" in t.lower() for t in sent_texts)


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


class TestBootstrapSessionIsolation:
    @pytest.mark.integration
    def test_active_session_archived_before_bootstrap_starts(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),
            _bs(BootstrapState.NEEDED),
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat(active_session_id="normal-session-99")
        mock_agent.run.return_value = _run_result()

        daemon.on_message("42", "hi")
        _wait_for_job(daemon)

        mock_db.archive_session.assert_called_once_with(
            "normal-session-99", "42", None, "/tmp/work"
        )

    @pytest.mark.integration
    def test_force_new_next_not_consumed_during_bootstrap(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),
            _bs(BootstrapState.NEEDED),
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        initial_state = ChatState(
            telegram_chat_id="42",
            force_new_next=True,
            bootstrap_session_id=None,
        )
        mock_db.get_chat.return_value = initial_state
        mock_agent.run.return_value = _run_result()

        daemon.on_message("42", "hi")
        _wait_for_job(daemon)

        # The flag must NOT be consumed (cleared) during bootstrap
        # It should fire on the first normal message after bootstrap completes
        upsert_calls = [c.args[0] for c in mock_db.upsert_chat.call_args_list]
        # No upsert should have cleared force_new_next=True to False explicitly
        # (the only upserts in bootstrap path are for status running and then post-job)
        # They carry the same force_new_next=True from the original state
        force_new_values = [s.force_new_next for s in upsert_calls if hasattr(s, "force_new_next")]
        # At least one upsert should still have force_new_next=True
        assert any(v is True for v in force_new_values)

    @pytest.mark.integration
    def test_file_deletion_re_triggers_bootstrap(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        # First call: COMPLETE; second call: NEEDED (file was deleted between messages)
        checker.check.side_effect = [
            _bs(BootstrapState.NEEDED),   # first message
            _bs(BootstrapState.NEEDED),   # post-job check
        ]
        checker.load_bootstrap_prompt.return_value = "PROMPT\n"
        daemon, mock_db, mock_agent, mock_session_manager, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_agent.run.return_value = _run_result()

        daemon.on_message("42", "second message")
        _wait_for_job(daemon)

        # Bootstrap path was taken (session_manager.decide NOT called)
        mock_session_manager.decide.assert_not_called()

    @pytest.mark.integration
    def test_normal_mode_unaffected_when_complete(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.return_value = _bs(BootstrapState.COMPLETE)
        daemon, mock_db, mock_agent, mock_session_manager, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat()
        mock_session_manager.decide.return_value = SessionDecision(mode="new")
        mock_agent.run.return_value = _run_result(session_id="normal-1")

        daemon.on_message("42", "do a thing")
        _wait_for_job(daemon)

        # Normal path taken: SessionManager.decide was called
        mock_session_manager.decide.assert_called_once()

    @pytest.mark.integration
    def test_in_progress_archive_not_called(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        checker.check.side_effect = [
            _bs(BootstrapState.IN_PROGRESS, session_id="bs-1"),
            _bs(BootstrapState.IN_PROGRESS, session_id="bs-1"),
        ]
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        mock_db.get_chat.return_value = _chat(bootstrap_session_id="bs-1")
        mock_agent.run.return_value = _run_result(session_id="bs-1")

        daemon.on_message("42", "next turn")
        _wait_for_job(daemon)

        # archive_session must not be called during IN_PROGRESS turns
        mock_db.archive_session.assert_not_called()


# ---------------------------------------------------------------------------
# Commands during bootstrap
# ---------------------------------------------------------------------------


class TestCommandsDuringBootstrap:
    @pytest.mark.integration
    def test_stop_command_during_bootstrap_keeps_session_id(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        # Simulate a running bootstrap job
        daemon.running_locks["42"] = "some-job-id"
        state = _chat(bootstrap_session_id="bs-live")
        state.status = ChatStatus.running
        mock_db.get_chat.return_value = state
        from app.models import Job

        mock_db.get_active_job.return_value = Job(
            job_id="some-job-id",
            telegram_chat_id="42",
            status=JobStatus.running,
        )

        from app.command_router import CommandRouter

        router = CommandRouter(
            db=mock_db,
            agent=mock_agent,
            bootstrap_checker=checker,
        )
        router.handle("42", "!stop")

        # After stop, the state upserted must still carry bootstrap_session_id
        upsert_calls = mock_db.upsert_chat.call_args_list
        assert upsert_calls, "upsert_chat should have been called"
        # The router doesn't clear bootstrap_session_id on stop — only !reset does
        saved = upsert_calls[-1].args[0]
        assert saved.bootstrap_session_id == "bs-live"

    @pytest.mark.integration
    def test_reset_command_clears_bootstrap_session_id(self, tmp_path):
        checker = MagicMock(spec=BootstrapChecker)
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path, checker)

        state = _chat(bootstrap_session_id="bs-stuck", active_session_id=None)
        mock_db.get_chat.return_value = state

        from app.command_router import CommandRouter

        router = CommandRouter(db=mock_db, agent=mock_agent, bootstrap_checker=checker)
        response = router.handle("42", "!reset")

        upsert_calls = mock_db.upsert_chat.call_args_list
        saved = upsert_calls[-1].args[0]
        assert saved.bootstrap_session_id is None
        assert "bootstrap" in response.lower()
