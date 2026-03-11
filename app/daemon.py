"""
daemon.py — Job lifecycle orchestration.

One chat = one running job at a time.  Background threads carry individual jobs.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from app import db, formatter
from app.claude_runner import ClaudeRunner
from app.models import ChatState, ChatStatus, Job, JobStatus, SessionDecision
from app.session_manager import SessionManager

logger = logging.getLogger(__name__)


class Daemon:
    """
    Orchestrates the full job lifecycle:
      receive message → create job → run in thread → send result to Telegram.
    """

    def __init__(
        self,
        db_module,
        runner: ClaudeRunner,
        session_manager: SessionManager,
        send_message_fn: Callable[[str, str], None],
        jobs_dir: str,
    ) -> None:
        """
        Parameters
        ----------
        db_module:        The app.db module (or compatible object with its functions).
        runner:           ClaudeRunner instance.
        session_manager:  SessionManager instance.
        send_message_fn:  Callable(chat_id, text) that delivers a message to Telegram.
        jobs_dir:         Directory where raw job output logs are written.
        """
        self._db = db_module
        self._runner = runner
        self._session_manager = session_manager
        self._send = send_message_fn
        self._jobs_dir = jobs_dir

        # chat_id → job_id for currently running jobs
        self.running_locks: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public entry point (called from the Telegram handler thread)
    # ------------------------------------------------------------------

    def on_message(self, chat_id: str, text: str) -> None:
        """
        Handle an incoming user message.

        If a job is already running for this chat, reply with a notice and return.
        Otherwise create a new job record and launch a background thread.
        """
        with self._lock:
            active_job_id = self.running_locks.get(chat_id)

        if active_job_id:
            logger.info(
                "Chat %s is busy (job=%s); rejecting new message", chat_id, active_job_id
            )
            self._send(
                chat_id,
                "当前任务仍在执行，请等待完成或发送 /stop 终止。",
            )
            return

        # Load (or create) chat state
        chat_state = self._db.get_chat(chat_id)
        if chat_state is None:
            chat_state = ChatState(telegram_chat_id=chat_id)

        # Honour the force_new_next flag
        force_new = chat_state.force_new_next
        if force_new:
            chat_state.force_new_next = False  # consume the flag
            self._db.upsert_chat(chat_state)

        # Decide session mode
        decision = self._session_manager.decide(chat_state, text, force_new=force_new)

        # Build job record
        job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{job_id}.log")

        job = Job(
            job_id=job_id,
            telegram_chat_id=chat_id,
            session_id=decision.session_id,
            user_message=text,
            status=JobStatus.queued,
        )
        self._db.create_job(job)

        # Lock the chat
        with self._lock:
            self.running_locks[chat_id] = job_id

        # Launch background thread
        thread = threading.Thread(
            target=self._run_job,
            args=(job, decision, chat_state, raw_output_path),
            daemon=True,
            name=f"job-{job_id[:8]}",
        )
        thread.start()
        logger.info("Launched thread for job %s (chat=%s)", job_id, chat_id)

    # ------------------------------------------------------------------
    # Background job execution
    # ------------------------------------------------------------------

    def _run_job(
        self,
        job: Job,
        decision: SessionDecision,
        chat_state: ChatState,
        raw_output_path: str,
    ) -> None:
        chat_id = job.telegram_chat_id

        try:
            # Mark job as running
            job.status = JobStatus.running
            job.started_at = datetime.now(timezone.utc).isoformat()
            self._db.update_job(job)

            # Update chat status
            chat_state.status = ChatStatus.running
            self._db.upsert_chat(chat_state)

            # Determine effective cwd
            cwd = chat_state.cwd or self._runner.work_dir
            task_name = chat_state.active_task_name or "未命名任务"

            # --- Phase 1: start notification ---
            self._send(
                chat_id,
                formatter.truncate_for_telegram(
                    formatter.format_start(task_name, decision.mode, cwd)
                ),
            )

            # --- Phase 2: running notification ---
            self._send(chat_id, formatter.format_running())

            # --- Phase 3: invoke Claude Code ---
            result = self._runner.run(
                prompt=job.user_message,
                cwd=cwd,
                session_id=decision.session_id,
                job_id=job.job_id,
            )

            # Persist raw output
            os.makedirs(os.path.dirname(raw_output_path), exist_ok=True)
            with open(raw_output_path, "w", encoding="utf-8") as fh:
                fh.write(result.stdout)
                if result.stderr:
                    fh.write("\n--- STDERR ---\n")
                    fh.write(result.stderr)

            # Update job record
            job.session_id = result.session_id
            job.status = JobStatus.done if result.exit_code == 0 else JobStatus.failed
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.exit_code = result.exit_code
            job.result_summary = result.summary
            job.raw_output_path = raw_output_path
            self._db.update_job(job)

            # Archive old session if switching to a new one
            if decision.mode == "new" and chat_state.active_session_id:
                self._db.archive_session(
                    chat_state.active_session_id,
                    chat_id,
                    chat_state.active_task_name,
                    cwd,
                )

            # Update chat state
            chat_state.active_session_id = result.session_id or chat_state.active_session_id
            chat_state.last_active_at = job.finished_at
            chat_state.last_summary = result.summary
            chat_state.status = ChatStatus.idle if result.exit_code == 0 else ChatStatus.error
            self._db.upsert_chat(chat_state)

            # --- Phase 4: result notification ---
            if result.exit_code == 0:
                msg = formatter.format_done(result.summary, result.exit_code, raw_output_path)
            else:
                msg = formatter.format_error(
                    result.summary or result.stderr[:500] or "(无错误信息)",
                    result.exit_code,
                )

            self._send(chat_id, formatter.truncate_for_telegram(msg))

        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s raised an exception: %s", job.job_id, exc)

            try:
                job.status = JobStatus.failed
                job.finished_at = datetime.now(timezone.utc).isoformat()
                job.exit_code = -1
                job.result_summary = str(exc)
                self._db.update_job(job)

                chat_state.status = ChatStatus.error
                self._db.upsert_chat(chat_state)

                self._send(
                    chat_id,
                    formatter.truncate_for_telegram(
                        formatter.format_error(str(exc), -1)
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to update db/Telegram after job error")

        finally:
            # Always release the lock
            with self._lock:
                self.running_locks.pop(chat_id, None)
            logger.info("Released lock for chat %s (job=%s)", chat_id, job.job_id)
