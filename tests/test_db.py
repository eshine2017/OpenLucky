"""Tests for app.db — SQLite persistence layer using in-memory database."""

import pytest

from app import db
from app.models import ChatState, ChatStatus, Job, JobStatus


@pytest.fixture(autouse=True)
def _init_db(tmp_path):
    """Initialize a fresh in-memory database for each test."""
    # Reset module-level connection
    db._conn = None
    db.init(":memory:")
    yield
    if db._conn:
        db._conn.close()
        db._conn = None


class TestInit:
    def test_tables_created(self) -> None:
        conn = db._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row["name"] for row in cur.fetchall()]
        assert "chats" in tables
        assert "jobs" in tables
        assert "session_history" in tables


class TestChatCRUD:
    def test_get_chat_nonexistent(self) -> None:
        assert db.get_chat("999") is None

    def test_upsert_and_get(self) -> None:
        state = ChatState(
            telegram_chat_id="100",
            active_session_id="s1",
            active_task_name="build",
            cwd="/tmp",
            status=ChatStatus.running,
            last_active_at="2025-01-01T00:00:00Z",
            last_summary="ok",
            force_new_next=True,
        )
        db.upsert_chat(state)

        loaded = db.get_chat("100")
        assert loaded is not None
        assert loaded.telegram_chat_id == "100"
        assert loaded.active_session_id == "s1"
        assert loaded.active_task_name == "build"
        assert loaded.cwd == "/tmp"
        assert loaded.status == ChatStatus.running
        assert loaded.last_active_at == "2025-01-01T00:00:00Z"
        assert loaded.last_summary == "ok"
        assert loaded.force_new_next is True

    def test_upsert_update(self) -> None:
        state = ChatState(telegram_chat_id="100", status=ChatStatus.idle)
        db.upsert_chat(state)

        state.status = ChatStatus.running
        state.active_session_id = "s2"
        db.upsert_chat(state)

        loaded = db.get_chat("100")
        assert loaded is not None
        assert loaded.status == ChatStatus.running
        assert loaded.active_session_id == "s2"


class TestJobCRUD:
    def test_create_and_get(self) -> None:
        job = Job(
            job_id="j1",
            telegram_chat_id="100",
            user_message="hello",
            status=JobStatus.queued,
        )
        db.create_job(job)

        loaded = db.get_job("j1")
        assert loaded is not None
        assert loaded.job_id == "j1"
        assert loaded.telegram_chat_id == "100"
        assert loaded.user_message == "hello"
        assert loaded.status == JobStatus.queued

    def test_get_job_nonexistent(self) -> None:
        assert db.get_job("nope") is None

    def test_update_job(self) -> None:
        job = Job(job_id="j2", telegram_chat_id="100", status=JobStatus.queued)
        db.create_job(job)

        job.status = JobStatus.running
        job.session_id = "s1"
        job.started_at = "2025-01-01T00:00:00Z"
        db.update_job(job)

        loaded = db.get_job("j2")
        assert loaded is not None
        assert loaded.status == JobStatus.running
        assert loaded.session_id == "s1"

    def test_get_active_job(self) -> None:
        # No active job
        assert db.get_active_job("100") is None

        # Create a running job
        job = Job(job_id="j3", telegram_chat_id="100", status=JobStatus.running)
        db.create_job(job)

        active = db.get_active_job("100")
        assert active is not None
        assert active.job_id == "j3"

    def test_get_active_job_ignores_done(self) -> None:
        job = Job(job_id="j4", telegram_chat_id="100", status=JobStatus.done)
        db.create_job(job)
        assert db.get_active_job("100") is None


class TestSessionHistory:
    def test_archive_and_get(self) -> None:
        db.archive_session("s1", "100", "task1", "/tmp")

        history = db.get_session_history("100")
        assert len(history) == 1
        assert history[0]["session_id"] == "s1"
        assert history[0]["telegram_chat_id"] == "100"
        assert history[0]["task_name"] == "task1"
        assert history[0]["is_archived"] == 1

    def test_archive_updates_existing(self) -> None:
        db.archive_session("s1", "100", "task1", "/tmp")
        db.archive_session("s1", "100", "task1", "/tmp")

        history = db.get_session_history("100")
        assert len(history) == 1

    def test_empty_history(self) -> None:
        assert db.get_session_history("999") == []
