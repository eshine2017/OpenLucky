"""Tests for app.command_router — command parsing and handler responses."""

from unittest.mock import MagicMock

import pytest

from app.command_router import CommandRouter
from app.models import ChatState, ChatStatus, Job, JobStatus


@pytest.fixture()
def mock_db():
    return MagicMock()


@pytest.fixture()
def mock_runner():
    return MagicMock()


@pytest.fixture()
def router(mock_db):
    return CommandRouter(db=mock_db, session_manager=MagicMock())


class TestIsCommand:
    def test_known_commands(self, router) -> None:
        assert router.is_command("/status") is True
        assert router.is_command("/stop") is True
        assert router.is_command("/new") is True
        assert router.is_command("/reset") is True
        assert router.is_command("/cwd /tmp") is True
        assert router.is_command("/task build") is True

    def test_unknown_command(self, router) -> None:
        assert router.is_command("/unknown") is False

    def test_not_a_command(self, router) -> None:
        assert router.is_command("hello") is False
        assert router.is_command("") is False

    def test_case_insensitive(self, router) -> None:
        assert router.is_command("/Status") is True
        assert router.is_command("/NEW") is True


class TestHandleStatus:
    def test_no_session(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "/status", mock_runner)
        assert "No active session" in result

    def test_with_session(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            active_task_name="build",
            cwd="/tmp",
            status=ChatStatus.running,
            last_active_at="2025-01-01T00:00:00Z",
        )
        result = router.handle("1", "/status", mock_runner)
        assert "running" in result
        assert "build" in result
        assert "/tmp" in result
        assert "s1" in result


class TestHandleStop:
    def test_no_active_job(self, router, mock_db, mock_runner) -> None:
        mock_db.get_active_job.return_value = None
        result = router.handle("1", "/stop", mock_runner)
        assert "No task is currently running" in result

    def test_cancel_active_job(self, router, mock_db, mock_runner) -> None:
        job = Job(job_id="j1-abcdef", telegram_chat_id="1", status=JobStatus.running)
        mock_db.get_active_job.return_value = job
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", status=ChatStatus.running)

        result = router.handle("1", "/stop", mock_runner)
        assert "Canceled" in result
        mock_runner.cancel.assert_called_once_with("j1-abcdef")
        mock_db.update_job.assert_called_once()
        mock_db.upsert_chat.assert_called_once()


class TestHandleNew:
    def test_sets_force_new(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1")
        result = router.handle("1", "/new", mock_runner)
        assert "new session" in result
        mock_db.upsert_chat.assert_called_once()
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.force_new_next is True

    def test_no_existing_chat(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "/new", mock_runner)
        assert "new session" in result


class TestHandleReset:
    def test_no_session(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "/reset", mock_runner)
        assert "No active session" in result

    def test_clear_session(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = ChatState(
            telegram_chat_id="1", active_session_id="s1-abcdef"
        )
        result = router.handle("1", "/reset", mock_runner)
        assert "cleared" in result.lower()
        mock_db.upsert_chat.assert_called_once()

    def test_no_bound_session(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", active_session_id=None)
        result = router.handle("1", "/reset", mock_runner)
        assert "No session was bound" in result


class TestHandleCwd:
    def test_no_path(self, router, mock_db, mock_runner) -> None:
        result = router.handle("1", "/cwd", mock_runner)
        assert "Usage" in result

    def test_relative_path(self, router, mock_db, mock_runner) -> None:
        result = router.handle("1", "/cwd relative/path", mock_runner)
        assert "absolute path" in result

    def test_valid_path(self, router, mock_db, mock_runner, tmp_path) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", cwd="/old")
        result = router.handle("1", f"/cwd {tmp_path}", mock_runner)
        assert "changed" in result.lower()
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.cwd == str(tmp_path)
        assert saved.force_new_next is True


class TestHandleTask:
    def test_no_name(self, router, mock_db, mock_runner) -> None:
        result = router.handle("1", "/task", mock_runner)
        assert "Usage" in result

    def test_set_name(self, router, mock_db, mock_runner) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", active_task_name="old")
        result = router.handle("1", "/task new-task", mock_runner)
        assert "new-task" in result
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.active_task_name == "new-task"
