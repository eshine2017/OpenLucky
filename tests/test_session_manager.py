"""Tests for app.session_manager — session decision logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.models import ChatState
from app.session_manager import SessionManager


def _make_manager(timeout_minutes: int = 30) -> SessionManager:
    return SessionManager(db=MagicMock(), timeout_minutes=timeout_minutes)


def _recent_time() -> str:
    return (datetime.now(UTC) - timedelta(minutes=5)).isoformat()


def _stale_time() -> str:
    return (datetime.now(UTC) - timedelta(minutes=60)).isoformat()


class TestDecide:
    def test_no_chat_state_new(self) -> None:
        sm = _make_manager()
        d = sm.decide(chat_state=None, text="hello")
        assert d.mode == "new"

    def test_no_active_session_new(self) -> None:
        sm = _make_manager()
        state = ChatState(telegram_chat_id="1", active_session_id=None)
        d = sm.decide(chat_state=state, text="hello")
        assert d.mode == "new"

    def test_force_new(self) -> None:
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=_recent_time(),
        )
        d = sm.decide(chat_state=state, text="hello", force_new=True)
        assert d.mode == "new"

    def test_timed_out_new(self) -> None:
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=_stale_time(),
        )
        d = sm.decide(chat_state=state, text="hello")
        assert d.mode == "new"

    def test_new_task_keywords(self) -> None:
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=_recent_time(),
        )
        d = sm.decide(chat_state=state, text="new task: build the frontend")
        assert d.mode == "new"

    def test_resume_within_timeout(self) -> None:
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=_recent_time(),
        )
        d = sm.decide(chat_state=state, text="add error handling")
        assert d.mode == "resume"
        assert d.session_id == "s1"

    def test_followup_overrides_new_task(self) -> None:
        """Follow-up keywords should win over new-task keywords."""
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=_recent_time(),
        )
        # Contains both "new task" and "continue"
        d = sm.decide(chat_state=state, text="continue the new task from before")
        assert d.mode == "resume"

    def test_no_last_active_at_timed_out(self) -> None:
        sm = _make_manager()
        state = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            last_active_at=None,
        )
        d = sm.decide(chat_state=state, text="hello")
        assert d.mode == "new"


class TestMessageIndicatesNewTask:
    def test_new_task_detected(self) -> None:
        sm = _make_manager()
        assert sm.message_indicates_new_task("new task: do something") is True
        assert sm.message_indicates_new_task("换个方向") is True
        assert sm.message_indicates_new_task("another thing") is True

    def test_normal_message_not_new(self) -> None:
        sm = _make_manager()
        assert sm.message_indicates_new_task("fix the bug") is False

    def test_followup_cancels_new(self) -> None:
        sm = _make_manager()
        assert sm.message_indicates_new_task("continue with another thing") is False

    def test_case_insensitive(self) -> None:
        sm = _make_manager()
        assert sm.message_indicates_new_task("New Task: build") is True
        assert sm.message_indicates_new_task("ANOTHER project") is True
