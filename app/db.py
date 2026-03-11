"""
db.py — SQLite persistence layer for openlucky.

Call db.init(db_path) before using any other function.
Thread-safe via a module-level lock and check_same_thread=False.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import UTC

from app.models import ChatState, ChatStatus, Job, JobStatus

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() must be called before using the database")
    return _conn


def init(db_path: str, data_dir: str | None = None) -> None:
    """
    Create the database and all tables if they do not already exist.

    Parameters
    ----------
    db_path:  Full path to the SQLite file.
    data_dir: Optional base directory under which ``jobs/`` and ``logs/``
              sub-directories will be created.  When omitted the parent of
              ``db_path`` is used (i.e. the ``data/`` directory in the normal
              project layout).
    """
    global _conn

    # Ensure parent directory of the db file exists
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Ensure data/jobs and data/logs directories exist
    base = data_dir or db_dir
    if base:
        os.makedirs(os.path.join(base, "jobs"), exist_ok=True)
        os.makedirs(os.path.join(base, "logs"), exist_ok=True)

    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row

    with _lock:
        cur = _conn.cursor()

        cur.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                telegram_chat_id TEXT PRIMARY KEY,
                active_session_id TEXT,
                active_task_name TEXT,
                cwd TEXT,
                status TEXT,
                last_active_at TEXT,
                last_summary TEXT,
                force_new_next INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                telegram_chat_id TEXT,
                session_id TEXT,
                user_message TEXT,
                status TEXT,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                result_summary TEXT,
                raw_output_path TEXT
            );

            CREATE TABLE IF NOT EXISTS session_history (
                session_id TEXT PRIMARY KEY,
                telegram_chat_id TEXT,
                task_name TEXT,
                cwd TEXT,
                created_at TEXT,
                last_active_at TEXT,
                is_archived INTEGER
            );
        """)
        _conn.commit()

    logger.info("Database initialised at %s", db_path)


# ---------------------------------------------------------------------------
# Chat helpers
# ---------------------------------------------------------------------------


def get_chat(chat_id: str) -> ChatState | None:
    with _lock:
        cur = _get_conn().cursor()
        cur.execute("SELECT * FROM chats WHERE telegram_chat_id = ?", (chat_id,))
        row = cur.fetchone()

    if row is None:
        return None

    return ChatState(
        telegram_chat_id=row["telegram_chat_id"],
        active_session_id=row["active_session_id"],
        active_task_name=row["active_task_name"],
        cwd=row["cwd"],
        status=ChatStatus(row["status"]) if row["status"] else ChatStatus.idle,
        last_active_at=row["last_active_at"],
        last_summary=row["last_summary"],
        force_new_next=bool(row["force_new_next"]),
    )


def upsert_chat(state: ChatState) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO chats
                (telegram_chat_id, active_session_id, active_task_name, cwd,
                 status, last_active_at, last_summary, force_new_next)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET
                active_session_id = excluded.active_session_id,
                active_task_name  = excluded.active_task_name,
                cwd               = excluded.cwd,
                status            = excluded.status,
                last_active_at    = excluded.last_active_at,
                last_summary      = excluded.last_summary,
                force_new_next    = excluded.force_new_next
            """,
            (
                state.telegram_chat_id,
                state.active_session_id,
                state.active_task_name,
                state.cwd,
                state.status.value if isinstance(state.status, ChatStatus) else state.status,
                state.last_active_at,
                state.last_summary,
                int(state.force_new_next),
            ),
        )
        conn.commit()

    logger.debug("Upserted chat %s (status=%s)", state.telegram_chat_id, state.status)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------


def create_job(job: Job) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO jobs
                (job_id, telegram_chat_id, session_id, user_message, status,
                 started_at, finished_at, exit_code, result_summary, raw_output_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.telegram_chat_id,
                job.session_id,
                job.user_message,
                job.status.value if isinstance(job.status, JobStatus) else job.status,
                job.started_at,
                job.finished_at,
                job.exit_code,
                job.result_summary,
                job.raw_output_path,
            ),
        )
        conn.commit()

    logger.debug("Created job %s for chat %s", job.job_id, job.telegram_chat_id)


def update_job(job: Job) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE jobs SET
                session_id      = ?,
                status          = ?,
                started_at      = ?,
                finished_at     = ?,
                exit_code       = ?,
                result_summary  = ?,
                raw_output_path = ?
            WHERE job_id = ?
            """,
            (
                job.session_id,
                job.status.value if isinstance(job.status, JobStatus) else job.status,
                job.started_at,
                job.finished_at,
                job.exit_code,
                job.result_summary,
                job.raw_output_path,
                job.job_id,
            ),
        )
        conn.commit()

    logger.debug("Updated job %s → status=%s", job.job_id, job.status)


def get_job(job_id: str) -> Job | None:
    with _lock:
        cur = _get_conn().cursor()
        cur.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()

    if row is None:
        return None

    return _row_to_job(row)


def get_active_job(chat_id: str) -> Job | None:
    """Return the currently running job for a chat, or None."""
    with _lock:
        cur = _get_conn().cursor()
        cur.execute(
            "SELECT * FROM jobs WHERE telegram_chat_id = ? AND status = 'running' LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return _row_to_job(row)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        telegram_chat_id=row["telegram_chat_id"],
        session_id=row["session_id"],
        user_message=row["user_message"],
        status=JobStatus(row["status"]) if row["status"] else JobStatus.queued,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        result_summary=row["result_summary"],
        raw_output_path=row["raw_output_path"],
    )


# ---------------------------------------------------------------------------
# Session history helpers
# ---------------------------------------------------------------------------


def archive_session(session_id: str, chat_id: str, task_name: str | None, cwd: str | None) -> None:
    from datetime import datetime

    now = datetime.now(UTC).isoformat()

    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO session_history
                (session_id, telegram_chat_id, task_name, cwd,
                 created_at, last_active_at, is_archived)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(session_id) DO UPDATE SET
                last_active_at = excluded.last_active_at,
                is_archived    = 1
            """,
            (session_id, chat_id, task_name, cwd, now, now),
        )
        conn.commit()

    logger.debug("Archived session %s for chat %s", session_id, chat_id)


def get_session_history(chat_id: str) -> list[dict]:
    with _lock:
        cur = _get_conn().cursor()
        cur.execute(
            "SELECT * FROM session_history WHERE telegram_chat_id = ? ORDER BY last_active_at DESC",
            (chat_id,),
        )
        rows = cur.fetchall()

    return [dict(row) for row in rows]
