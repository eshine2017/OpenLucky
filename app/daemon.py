"""
daemon.py — Job lifecycle orchestration.

One chat = one running job at a time.  Background threads carry individual jobs.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app import formatter
from app.agents.base import BaseAgent
from app.bootstrap import BootstrapChecker, BootstrapState, BootstrapStatus, is_complete_signal
from app.context_builder import ContextBuilder
from app.models import ChatState, ChatStatus, Job, JobStatus, RunResult, SessionDecision
from app.session_manager import SessionManager

logger = logging.getLogger(__name__)


class Daemon:
    """
    Orchestrates the full job lifecycle:
      receive message → create job → run in thread → send result to Telegram.
    """

    def __init__(
        self,
        db_module: Any,
        agent: BaseAgent,
        session_manager: SessionManager,
        send_message_fn: Callable[[str, str], None],
        jobs_dir: str,
        default_cwd: str = "/tmp/openlucky_work",
        context_builder: ContextBuilder | None = None,
        bootstrap_checker: BootstrapChecker | None = None,
    ) -> None:
        self._db = db_module
        self._agent = agent
        self._session_manager = session_manager
        self._send = send_message_fn
        self._jobs_dir = jobs_dir
        self._default_cwd = default_cwd
        self._context_builder = context_builder
        self._bootstrap_checker = bootstrap_checker

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
            logger.info("Chat %s is busy (job=%s); rejecting new message", chat_id, active_job_id)
            self._send(
                chat_id,
                "A task is already running. Wait for it to finish or send !stop to cancel.",
            )
            return

        # Load (or create) chat state
        chat_state = self._db.get_chat(chat_id)
        if chat_state is None:
            chat_state = ChatState(telegram_chat_id=chat_id)

        # Bootstrap check — evaluated on every message before normal session logic.
        # force_new_next and SessionManager are bypassed when bootstrap is active.
        if self._bootstrap_checker is not None:
            bs = self._bootstrap_checker.check(chat_state)
            if bs.state != BootstrapState.COMPLETE:
                if bs.state == BootstrapState.NEEDED:
                    logger.info(
                        "bootstrap: re-triggered for chat %s (soul=%s user=%s)",
                        chat_id, bs.soul, bs.user,
                    )
                self._launch_bootstrap_job(chat_id, text, chat_state, bs)
                return

        # Normal path — honour the force_new_next flag then decide session mode
        force_new = chat_state.force_new_next
        if force_new:
            chat_state.force_new_next = False  # consume the flag
            self._db.upsert_chat(chat_state)

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
    # Bootstrap job launch
    # ------------------------------------------------------------------

    def _launch_bootstrap_job(
        self,
        chat_id: str,
        text: str,
        chat_state: ChatState,
        bs: BootstrapStatus,
    ) -> None:
        """Prepare and launch a bootstrap job in a background thread."""
        if self._bootstrap_checker is None:
            raise RuntimeError("_launch_bootstrap_job called without a bootstrap_checker")

        # Archive any existing normal session before starting fresh bootstrap
        if bs.state == BootstrapState.NEEDED and chat_state.active_session_id:
            cwd = chat_state.cwd or self._default_cwd
            self._db.archive_session(
                chat_state.active_session_id,
                chat_id,
                chat_state.active_task_name,
                cwd,
            )
            chat_state = replace(chat_state, active_session_id=None)
            self._db.upsert_chat(chat_state)
            logger.info("bootstrap: archived existing session for chat %s", chat_id)

        if bs.state == BootstrapState.NEEDED:
            logger.info("bootstrap: NEEDED → starting new session for chat %s", chat_id)
            prompt = (
                self._bootstrap_checker.load_bootstrap_prompt()
                + "\n\n# First message\n"
                + text
            )
            session_id = None
            self._send(
                chat_id,
                "First-time setup — let me ask a few quick questions to get to know you.",
            )
        else:
            logger.info(
                "bootstrap: IN_PROGRESS → resuming session %s for chat %s",
                bs.session_id,
                chat_id,
            )
            prompt = text
            session_id = bs.session_id

        decision = SessionDecision(
            mode="new" if session_id is None else "resume",
            session_id=session_id,
        )

        job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{job_id}.log")
        job = Job(
            job_id=job_id,
            telegram_chat_id=chat_id,
            session_id=session_id,
            user_message=text,
            status=JobStatus.queued,
        )
        self._db.create_job(job)

        with self._lock:
            self.running_locks[chat_id] = job_id

        thread = threading.Thread(
            target=self._run_job,
            args=(job, decision, chat_state, raw_output_path),
            kwargs={"is_bootstrap": True, "bootstrap_prompt": prompt},
            daemon=True,
            name=f"bootstrap-{job_id[:8]}",
        )
        thread.start()
        logger.info("Launched bootstrap thread for job %s (chat=%s)", job_id, chat_id)

    # ------------------------------------------------------------------
    # Prompt construction (normal path)
    # ------------------------------------------------------------------

    def _build_prompt(self, user_message: str, mode: str) -> str:
        if self._context_builder is None:
            return user_message
        try:
            if mode == "new":
                prefix = self._context_builder.build_prefix()
                if prefix:
                    return f"{prefix}\n\n---\n\n# Task\n{user_message}"
            else:
                hint = self._context_builder.build_resume_hint()
                if hint:
                    return f"{hint}\n\n{user_message}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Context builder failed, using raw message: %s", exc)
        return user_message

    # ------------------------------------------------------------------
    # Background job execution
    # ------------------------------------------------------------------

    def _run_job(
        self,
        job: Job,
        decision: SessionDecision,
        chat_state: ChatState,
        raw_output_path: str,
        is_bootstrap: bool = False,
        bootstrap_prompt: str | None = None,
    ) -> None:
        chat_id = job.telegram_chat_id

        try:
            # Mark job as running
            job = replace(job, status=JobStatus.running, started_at=datetime.now(UTC).isoformat())
            self._db.update_job(job)

            # Update chat status
            chat_state = replace(chat_state, status=ChatStatus.running)
            self._db.upsert_chat(chat_state)

            cwd = chat_state.cwd or self._default_cwd
            task_name = chat_state.active_task_name or "untitled"

            if not is_bootstrap:
                # Start + running notifications (suppressed for bootstrap — we already
                # sent "First-time setup..." from on_message / _launch_bootstrap_job)
                self._send(
                    chat_id,
                    formatter.truncate_for_telegram(
                        formatter.format_start(task_name, decision.mode, cwd)
                    ),
                )
                self._send(chat_id, formatter.format_running())

            # Build prompt
            if is_bootstrap:
                prompt = bootstrap_prompt or job.user_message
            else:
                prompt = self._build_prompt(job.user_message, decision.mode)

            # Invoke agent
            result = self._agent.run(
                prompt=prompt,
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
            job = replace(
                job,
                session_id=result.session_id,
                status=JobStatus.done if result.exit_code == 0 else JobStatus.failed,
                finished_at=datetime.now(UTC).isoformat(),
                exit_code=result.exit_code,
                result_summary=result.summary,
                raw_output_path=raw_output_path,
            )
            self._db.update_job(job)

            if is_bootstrap:
                self._handle_bootstrap_result(job, chat_state, result)
            else:
                # Archive old session if switching to a new one
                if decision.mode == "new" and chat_state.active_session_id:
                    self._db.archive_session(
                        chat_state.active_session_id,
                        chat_id,
                        chat_state.active_task_name,
                        cwd,
                    )

                # Update chat state; on error, clear session so next message starts fresh
                success = result.exit_code == 0
                chat_state = replace(
                    chat_state,
                    active_session_id=result.session_id if success else None,
                    last_active_at=job.finished_at,
                    last_summary=result.summary,
                    status=ChatStatus.idle if success else ChatStatus.error,
                )
                self._db.upsert_chat(chat_state)

                if result.exit_code == 0:
                    msg = formatter.format_done(result.summary, result.exit_code, raw_output_path)
                else:
                    msg = formatter.format_error(
                        result.summary or result.stderr[:500] or "(no error output)",
                        result.exit_code,
                    )
                self._send(chat_id, formatter.truncate_for_telegram(msg))

        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s raised an exception: %s", job.job_id, exc)

            try:
                job = replace(
                    job,
                    status=JobStatus.failed,
                    finished_at=datetime.now(UTC).isoformat(),
                    exit_code=-1,
                    result_summary=str(exc),
                )
                self._db.update_job(job)

                chat_state = replace(chat_state, status=ChatStatus.error)
                self._db.upsert_chat(chat_state)

                self._send(
                    chat_id,
                    formatter.truncate_for_telegram(formatter.format_error(str(exc), -1)),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to update db/Telegram after job error")

        finally:
            # Always release the lock
            with self._lock:
                self.running_locks.pop(chat_id, None)
            logger.info("Released lock for chat %s (job=%s)", chat_id, job.job_id)

    def _handle_bootstrap_result(
        self,
        job: Job,
        chat_state: ChatState,
        result: RunResult,
    ) -> None:
        """Post-job handler for bootstrap turns. Never touches active_session_id."""
        chat_id = job.telegram_chat_id
        new_bootstrap_session_id = result.session_id or chat_state.bootstrap_session_id

        if result.exit_code != 0:
            logger.warning(
                "bootstrap: job failed (exit %d) for chat %s; keeping session_id for resume",
                result.exit_code,
                chat_id,
            )
            chat_state = replace(
                chat_state,
                bootstrap_session_id=new_bootstrap_session_id,
                status=ChatStatus.idle,
                last_active_at=job.finished_at,
            )
            self._db.upsert_chat(chat_state)
            self._send(chat_id, "Something went wrong during setup. Try again when ready.")
            return

        # Persist session_id immediately so a crash after this point doesn't lose it
        chat_state = replace(chat_state, bootstrap_session_id=new_bootstrap_session_id)
        self._db.upsert_chat(chat_state)

        # Both signals required for completion: sentinel + file verification
        if (
            self._bootstrap_checker is not None
            and is_complete_signal(result.stdout, result.summary)
            and self._bootstrap_checker.check(chat_state).state == BootstrapState.COMPLETE
        ):
            logger.info("bootstrap: COMPLETE for chat %s", chat_id)
            chat_state = replace(
                chat_state,
                bootstrap_session_id=None,
                active_session_id=None,
                status=ChatStatus.idle,
                last_active_at=job.finished_at,
                last_summary=result.summary,
            )
            self._db.upsert_chat(chat_state)
            self._send(
                chat_id,
                "Setup complete! You can chat normally now.\n"
                "(Your memory file fills as we work together — try !memory later.)",
            )
            return

        # Still in progress — send Claude's response as the next bootstrap turn reply
        chat_state = replace(
            chat_state,
            status=ChatStatus.idle,
            last_active_at=job.finished_at,
            last_summary=result.summary,
        )
        self._db.upsert_chat(chat_state)
        self._send(
            chat_id,
            formatter.truncate_for_telegram(result.summary or "(no response)"),
        )
