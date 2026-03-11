"""Tests for app.models — enums, dataclass defaults, SessionDecision."""

from app.models import ChatState, ChatStatus, Job, JobStatus, RunResult, SessionDecision


class TestChatStatus:
    def test_values(self) -> None:
        assert ChatStatus.idle.value == "idle"
        assert ChatStatus.running.value == "running"
        assert ChatStatus.error.value == "error"

    def test_str_enum(self) -> None:
        assert str(ChatStatus.idle) == "idle"
        assert ChatStatus("idle") is ChatStatus.idle


class TestJobStatus:
    def test_values(self) -> None:
        assert JobStatus.queued.value == "queued"
        assert JobStatus.running.value == "running"
        assert JobStatus.done.value == "done"
        assert JobStatus.failed.value == "failed"
        assert JobStatus.canceled.value == "canceled"

    def test_str_enum(self) -> None:
        assert JobStatus("done") is JobStatus.done


class TestChatState:
    def test_defaults(self) -> None:
        cs = ChatState(telegram_chat_id="123")
        assert cs.telegram_chat_id == "123"
        assert cs.active_session_id is None
        assert cs.active_task_name is None
        assert cs.cwd is None
        assert cs.status == ChatStatus.idle
        assert cs.last_active_at is None
        assert cs.last_summary is None
        assert cs.force_new_next is False

    def test_custom_values(self) -> None:
        cs = ChatState(
            telegram_chat_id="456",
            active_session_id="sess-1",
            status=ChatStatus.running,
            force_new_next=True,
        )
        assert cs.active_session_id == "sess-1"
        assert cs.status == ChatStatus.running
        assert cs.force_new_next is True


class TestJob:
    def test_defaults(self) -> None:
        job = Job(job_id="j1", telegram_chat_id="123")
        assert job.session_id is None
        assert job.user_message == ""
        assert job.status == JobStatus.queued
        assert job.exit_code is None
        assert job.result_summary is None


class TestRunResult:
    def test_fields(self) -> None:
        rr = RunResult(session_id="s1", stdout="out", stderr="err", exit_code=0, summary="done")
        assert rr.session_id == "s1"
        assert rr.exit_code == 0


class TestSessionDecision:
    def test_new(self) -> None:
        sd = SessionDecision(mode="new")
        assert sd.mode == "new"
        assert sd.session_id is None

    def test_resume(self) -> None:
        sd = SessionDecision(mode="resume", session_id="abc")
        assert sd.mode == "resume"
        assert sd.session_id == "abc"
