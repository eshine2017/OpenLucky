"""Tests for Daemon.run_scheduled_job — Scheduled job execution path."""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import MagicMock

from app.bootstrap import BootstrapState, BootstrapStatus
from app.daemon import Daemon
from app.models import ChatState, ChatStatus, RunResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_result(
    *,
    exit_code: int = 0,
    summary: str = "Morning digest done",
    session_id: str = "sched-session",
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


def _idle_chat(chat_id: str = "42") -> ChatState:
    return ChatState(
        telegram_chat_id=chat_id,
        status=ChatStatus.idle,
        active_session_id="prev-session",
        cwd="/tmp/work",
    )


# ---------------------------------------------------------------------------
# run_scheduled_job — skip cases
# ---------------------------------------------------------------------------


class TestRunScheduledJobSkips:
    def test_skips_when_no_chat(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = None

        result = daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        assert result == "skipped:no_chat"
        mock_agent.run.assert_not_called()

    def test_skips_when_bootstrap_incomplete(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()

        mock_bootstrap = MagicMock()
        mock_bootstrap.check.return_value = BootstrapStatus(
            state=BootstrapState.NEEDED,
            session_id=None,
            soul=False,
            user=False,
        )
        daemon._bootstrap_checker = mock_bootstrap

        result = daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        assert result == "skipped:bootstrap"
        mock_agent.run.assert_not_called()

    def test_skips_when_chat_busy(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()

        # Simulate a busy chat
        daemon.running_locks["42"] = "existing-job-id"

        result = daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        assert result == "skipped:busy"
        mock_agent.run.assert_not_called()


# ---------------------------------------------------------------------------
# run_scheduled_job — happy path
# ---------------------------------------------------------------------------


class TestRunScheduledJobHappy:
    def test_returns_dispatched(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result()

        result = daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        assert result == "dispatched"

    def test_spawns_thread_and_runs_agent(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result()

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        # Wait for background thread to finish
        deadline = time.time() + 3
        while time.time() < deadline:
            if mock_agent.run.called:
                break
            time.sleep(0.05)

        mock_agent.run.assert_called_once()
        call_kwargs = mock_agent.run.call_args[1]
        assert call_kwargs["prompt"] == "Good morning"

    def test_does_not_mutate_chat_session_id(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        original_session = "user-interactive-session"
        chat = replace(_idle_chat(), active_session_id=original_session)
        mock_db.get_most_recent_chat.return_value = chat
        mock_agent.run.return_value = _make_run_result(session_id="new-sched-session")

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        # Wait for thread
        deadline = time.time() + 3
        while time.time() < deadline:
            if mock_agent.run.called:
                break
            time.sleep(0.05)

        # upsert_chat must NOT have been called with a changed session_id
        for call in mock_db.upsert_chat.call_args_list:
            saved_state: ChatState = call[0][0]
            assert saved_state.active_session_id == original_session, (
                "Scheduled job must not overwrite active_session_id"
            )

    def test_creates_job_with_kind_scheduled(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result()

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        # Wait for thread
        deadline = time.time() + 3
        while time.time() < deadline:
            if mock_db.create_job.called:
                break
            time.sleep(0.05)

        created_job = mock_db.create_job.call_args[0][0]
        assert created_job.kind == "scheduled"

    def test_sends_summary_to_telegram(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, mock_send = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result(summary="Today's digest summary")

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        # Wait for thread
        deadline = time.time() + 3
        while time.time() < deadline:
            if mock_send.called:
                break
            time.sleep(0.05)

        assert mock_send.called
        # Summary should appear in one of the messages
        all_messages = " ".join(str(c) for c in mock_send.call_args_list)
        assert "Today's digest summary" in all_messages

    def test_uses_session_id_none(self, tmp_path) -> None:
        """Scheduled runs always start a fresh Claude session."""
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result()

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        deadline = time.time() + 3
        while time.time() < deadline:
            if mock_agent.run.called:
                break
            time.sleep(0.05)

        call_kwargs = mock_agent.run.call_args[1]
        assert call_kwargs.get("session_id") is None

    def test_releases_lock_after_completion(self, tmp_path) -> None:
        daemon, mock_db, mock_agent, _, _ = _make_daemon(tmp_path)
        mock_db.get_most_recent_chat.return_value = _idle_chat()
        mock_agent.run.return_value = _make_run_result()

        daemon.run_scheduled_job(
            prompt="Good morning",
            label="morning-digest",
        )

        deadline = time.time() + 3
        while time.time() < deadline:
            if "42" not in daemon.running_locks:
                break
            time.sleep(0.05)

        assert "42" not in daemon.running_locks
