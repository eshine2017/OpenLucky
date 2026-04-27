"""Tests for app.db — SQLite persistence layer using in-memory database."""

import sqlite3

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


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigration:
    def test_bootstrap_session_id_column_exists_after_init(self) -> None:
        conn = db._get_conn()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(chats)")
        cols = [row[1] for row in cur.fetchall()]
        assert "bootstrap_session_id" in cols

    def test_migration_adds_column_to_existing_db(self, tmp_path) -> None:
        db_path = str(tmp_path / "old.db")

        # Create an old-schema DB without the bootstrap_session_id column
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE chats (
                telegram_chat_id TEXT PRIMARY KEY,
                active_session_id TEXT,
                active_task_name TEXT,
                cwd TEXT,
                status TEXT,
                last_active_at TEXT,
                last_summary TEXT,
                force_new_next INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO chats (telegram_chat_id) VALUES ('999')")
        conn.commit()
        conn.close()

        # Now run init() on the old DB — migration should add the column
        db._conn = None
        db.init(db_path)

        cur = db._get_conn().cursor()
        cur.execute("PRAGMA table_info(chats)")
        cols = [row[1] for row in cur.fetchall()]
        assert "bootstrap_session_id" in cols

        # Existing row survives; new column defaults to NULL
        loaded = db.get_chat("999")
        assert loaded is not None
        assert loaded.bootstrap_session_id is None

    def test_migration_idempotent(self) -> None:
        # Running init() again (which calls _migrate) must not raise
        conn = db._get_conn()
        db._migrate(conn)  # second call — column already exists
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(chats)")
        cols = [row[1] for row in cur.fetchall()]
        assert cols.count("bootstrap_session_id") == 1

    def test_existing_user_with_filled_files_sees_complete_state(self, tmp_path) -> None:
        """Existing user upgrading: filled workspace + NULL bootstrap_session_id → COMPLETE."""
        from app.bootstrap import BootstrapChecker, BootstrapState

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("Custom soul content", encoding="utf-8")
        (workspace / "USER.md").write_text("Custom user content", encoding="utf-8")

        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "SOUL.md").write_text("Template soul", encoding="utf-8")
        (templates_dir / "USER.md").write_text("Template user", encoding="utf-8")

        checker = BootstrapChecker(str(workspace), str(templates_dir))

        # Simulate an existing user row with NULL bootstrap_session_id
        state = ChatState(telegram_chat_id="existing-user", bootstrap_session_id=None)
        bs = checker.check(state)
        assert bs.state == BootstrapState.COMPLETE

    def test_bootstrap_session_id_persisted_and_read(self) -> None:
        state = ChatState(
            telegram_chat_id="200",
            bootstrap_session_id="bs-abc-123",
        )
        db.upsert_chat(state)

        loaded = db.get_chat("200")
        assert loaded is not None
        assert loaded.bootstrap_session_id == "bs-abc-123"

    def test_bootstrap_session_id_nullable(self) -> None:
        state = ChatState(telegram_chat_id="201", bootstrap_session_id=None)
        db.upsert_chat(state)

        loaded = db.get_chat("201")
        assert loaded is not None
        assert loaded.bootstrap_session_id is None
