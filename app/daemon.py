"""
daemon.py — Job lifecycle orchestration.

One chat = one running job at a time.  Background threads carry individual jobs.
"""

from __future__ import annotations

import json
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
from app.agents.registry import AgentRegistry
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
        agent: BaseAgent | None = None,
        session_manager: SessionManager = None,  # type: ignore[assignment]
        send_message_fn: Callable[[str, str], None] = None,  # type: ignore[assignment]
        jobs_dir: str = "",
        default_cwd: str = "/tmp/openlucky_work",
        context_builder: ContextBuilder | None = None,
        bootstrap_checker: BootstrapChecker | None = None,
        cron_spec_path: str = "",
        registry: AgentRegistry | None = None,
    ) -> None:
        self._db = db_module
        self._agent = agent
        self._registry = registry
        self._session_manager = session_manager
        self._send = send_message_fn
        self._jobs_dir = jobs_dir
        self._default_cwd = default_cwd
        self._context_builder = context_builder
        self._bootstrap_checker = bootstrap_checker
        self._cron_spec_path = cron_spec_path

        # chat_id → job_id for currently running jobs
        self.running_locks: dict[str, str] = {}
        self._lock = threading.Lock()

        # Pending one-shot actions set by command handlers
        self.pending_actions: dict[str, str] = {}

    def _resolve_agent(self, provider: str | None) -> BaseAgent:
        """Return the right agent for *provider*, falling back gracefully."""
        if self._registry is not None:
            return self._registry.get(provider)
        return self._agent  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public entry point (called from the Telegram handler thread)
    # ------------------------------------------------------------------

    def on_message(self, chat_id: str, text: str) -> None:
        """
        Handle an incoming user text message.

        If a job is already running for this chat, reply with a notice and return.
        Otherwise create a new job record and launch a background thread.
        """
        # Check for a pending one-shot action before the busy check
        pending = self.pending_actions.pop(chat_id, None)
        if pending == "schedule_add":
            self._handle_schedule_add(chat_id, text)
            return
        if pending and pending.startswith("schedule_update:"):
            job_id = pending.split(":", 1)[1]
            self._handle_schedule_update(chat_id, job_id, text)
            return

        self._dispatch_user_turn(chat_id, text, [])

    def on_photo_message(self, chat_id: str, caption: str, image_paths: list[str]) -> None:
        """Handle an incoming photo message (with optional caption)."""
        pending = self.pending_actions.pop(chat_id, None)
        if pending == "schedule_add":
            self._handle_schedule_add(chat_id, caption)
            return
        if pending and pending.startswith("schedule_update:"):
            job_id = pending.split(":", 1)[1]
            self._handle_schedule_update(chat_id, job_id, caption)
            return
        self._dispatch_user_turn(chat_id, caption, image_paths)

    def _dispatch_user_turn(
        self, chat_id: str, text: str, image_paths: list[str]
    ) -> None:
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
                        chat_id,
                        bs.soul,
                        bs.user,
                    )
                self._launch_bootstrap_job(chat_id, text, chat_state, bs)
                return

        # Normal path — honour the force_new_next flag then decide session mode.
        # For photos, pass "" to SessionManager so keyword heuristics are bypassed:
        # an active session always resumes; no session always starts new.
        force_new = chat_state.force_new_next
        if force_new:
            chat_state = replace(chat_state, force_new_next=False)
            self._db.upsert_chat(chat_state)

        decision_text = "" if image_paths else text
        decision = self._session_manager.decide(chat_state, decision_text, force_new=force_new)

        # Build job record
        job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{job_id}.log")

        job = Job(
            job_id=job_id,
            telegram_chat_id=chat_id,
            session_id=decision.session_id,
            user_message=text,
            status=JobStatus.queued,
            image_paths=image_paths,
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
    # Scheduled job entry point
    # ------------------------------------------------------------------

    def run_scheduled_job(
        self,
        *,
        prompt: str,
        label: str,
    ) -> str:
        """
        Dispatch a scheduled (non-interactive) job.

        Finds the most recent chat automatically.
        Returns one of: "dispatched", "skipped:busy",
        "skipped:bootstrap", "skipped:no_chat".

        Critical: the background thread must NOT mutate ChatState.active_session_id,
        last_active_at, or last_summary — scheduled runs are transient and must not
        corrupt the user's interactive session.
        """
        chat = self._db.get_most_recent_chat()
        if chat is None:
            logger.info("Scheduled job %r skipped: no chat in DB", label)
            return "skipped:no_chat"

        chat_id = chat.telegram_chat_id
        chat_state = chat

        if self._bootstrap_checker is not None:
            bs = self._bootstrap_checker.check(chat_state)
            if bs.state != BootstrapState.COMPLETE:
                logger.info("Scheduled job %r skipped: bootstrap incomplete for %s", label, chat_id)
                return "skipped:bootstrap"

        prefixed_prompt = self._build_scheduled_prompt(prompt)
        cwd = chat_state.cwd or self._default_cwd

        job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{job_id}.log")
        job = Job(
            job_id=job_id,
            telegram_chat_id=chat_id,
            session_id=None,
            user_message=f"[{label}]",
            status=JobStatus.queued,
            kind="scheduled",
        )

        with self._lock:
            if chat_id in self.running_locks:
                logger.info("Scheduled job %r skipped: chat %s is busy", label, chat_id)
                return "skipped:busy"
            self.running_locks[chat_id] = job_id

        try:
            self._db.create_job(job)
        except Exception:
            with self._lock:
                self.running_locks.pop(chat_id, None)
            raise

        thread = threading.Thread(
            target=self._run_scheduled_job_thread,
            args=(job, chat_state, prefixed_prompt, cwd, raw_output_path),
            daemon=True,
            name=f"sched-{label}-{job_id[:8]}",
        )
        thread.start()
        logger.info("Launched scheduled job %s label=%r (chat=%s)", job_id, label, chat_id)
        return "dispatched"

    def _run_scheduled_job_thread(
        self,
        job: Job,
        chat_state: ChatState,
        prompt: str,
        cwd: str,
        raw_output_path: str,
    ) -> None:
        chat_id = job.telegram_chat_id

        try:
            job = replace(job, status=JobStatus.running, started_at=datetime.now(UTC).isoformat())
            self._db.update_job(job)

            agent = self._resolve_agent(chat_state.provider)
            result = agent.run(
                prompt=prompt,
                cwd=cwd,
                session_id=None,
                job_id=job.job_id,
            )

            os.makedirs(os.path.dirname(raw_output_path), exist_ok=True)
            with open(raw_output_path, "w", encoding="utf-8") as fh:
                fh.write(result.stdout)
                if result.stderr:
                    fh.write("\n--- STDERR ---\n")
                    fh.write(result.stderr)

            job = replace(
                job,
                status=JobStatus.done if result.exit_code == 0 else JobStatus.failed,
                finished_at=datetime.now(UTC).isoformat(),
                exit_code=result.exit_code,
                result_summary=result.summary,
                raw_output_path=raw_output_path,
            )
            self._db.update_job(job)

            if result.exit_code == 0:
                msg = formatter.truncate_for_telegram(result.summary or "(no summary)")
            else:
                msg = formatter.truncate_for_telegram(
                    formatter.format_error(
                        result.summary or result.stderr[:500] or "(no error output)",
                        result.exit_code,
                    )
                )
            self._send(chat_id, msg)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled job %s raised an exception: %s", job.job_id, exc)
            try:
                job = replace(
                    job,
                    status=JobStatus.failed,
                    finished_at=datetime.now(UTC).isoformat(),
                    exit_code=-1,
                    result_summary=str(exc),
                )
                self._db.update_job(job)
                self._send(
                    chat_id,
                    formatter.truncate_for_telegram(formatter.format_error(str(exc), -1)),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to update db/Telegram after scheduled job error")

        finally:
            with self._lock:
                self.running_locks.pop(chat_id, None)
            logger.info("Released lock for chat %s (scheduled job=%s)", chat_id, job.job_id)

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
                self._bootstrap_checker.load_bootstrap_prompt(bs) + "\n\n# First message\n" + text
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

    def _build_scheduled_prompt(self, prompt: str) -> str:
        """Build a prompt for a scheduled job, prepending context if available."""
        if self._context_builder is None:
            return prompt
        try:
            prefix = self._context_builder.build_prefix()
            if prefix:
                return f"{prefix}\n\n---\n\n# Scheduled Task\n{prompt}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Context builder failed for scheduled job: %s", exc)
        return prompt

    def _build_schedule_add_prompt(self, user_request: str) -> str:
        """Build the prompt for Claude to add a scheduled job to cron.json."""
        prefix = ""
        tz = "ASK_USER"
        if self._context_builder is not None:
            try:
                prefix = self._context_builder.build_prefix()
                from app.context_builder import read_user_timezone

                tz_val = read_user_timezone(self._context_builder.workspace_dir)
                if tz_val:
                    tz = tz_val
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read context for schedule add: %s", exc)

        cron_path = self._cron_spec_path or "(cron.json path not configured)"

        task_instructions = (
            "# Task: add a recurring scheduled job\n\n"
            f"Schedule definitions live at: {cron_path}\n\n"
            "Schema (one entry per job, JSON array under `jobs`):\n"
            "  id          short-snake-case-id (must be unique)\n"
            "  name        human-readable name\n"
            "  enabled     true\n"
            "  cron_expr   5-field cron expression\n"
            f"  tz          IANA timezone (pre-filled: {tz})\n"
            "  prompt      the full prompt to send when fired — self-contained,\n"
            "              because SOUL/USER/MEMORY context is auto-prepended\n\n"
            "Read the file (create if missing), append a new entry, write it back.\n"
            "If `tz` above is ASK_USER, ask the user for their timezone before writing.\n"
            "If anything else is unclear (timing, sources, frequency), ask the user.\n"
            "Confirm with a one-line summary when done.\n\n"
            f"User request:\n{user_request}"
        )
        if prefix:
            return f"{prefix}\n\n---\n\n{task_instructions}"
        return task_instructions

    def _handle_schedule_add(self, chat_id: str, user_request: str) -> None:
        """Handle the follow-up message after '!schedule add'."""
        with self._lock:
            busy = chat_id in self.running_locks

        if busy:
            self._send(
                chat_id,
                "A task is already running. Try !schedule add again when free.",
            )
            return

        prompt = self._build_schedule_add_prompt(user_request)

        chat_state = self._db.get_chat(chat_id)
        if chat_state is None:
            chat_state = ChatState(telegram_chat_id=chat_id)

        cwd = chat_state.cwd or self._default_cwd

        job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{job_id}.log")
        job = Job(
            job_id=job_id,
            telegram_chat_id=chat_id,
            session_id=None,
            user_message=f"[schedule-add] {user_request[:80]}",
            status=JobStatus.queued,
        )

        with self._lock:
            if chat_id in self.running_locks:
                self._send(
                    chat_id,
                    "A task is already running. Try !schedule add again when free.",
                )
                return
            self.running_locks[chat_id] = job_id

        try:
            self._db.create_job(job)
        except Exception:
            with self._lock:
                self.running_locks.pop(chat_id, None)
            raise

        thread = threading.Thread(
            target=self._run_scheduled_job_thread,
            args=(job, chat_state, prompt, cwd, raw_output_path),
            daemon=True,
            name=f"schedule-add-{job_id[:8]}",
        )
        thread.start()
        logger.info("Launched schedule-add job %s (chat=%s)", job_id, chat_id)

    def _build_schedule_update_prompt(
        self, job_id: str, current_job_json: str, user_request: str
    ) -> str:
        """Build the prompt for Claude to update an existing scheduled job in cron.json."""
        prefix = ""
        tz = "ASK_USER"
        if self._context_builder is not None:
            try:
                prefix = self._context_builder.build_prefix()
                from app.context_builder import read_user_timezone

                tz_val = read_user_timezone(self._context_builder.workspace_dir)
                if tz_val:
                    tz = tz_val
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read context for schedule update: %s", exc)

        cron_path = self._cron_spec_path or "(cron.json path not configured)"

        task_instructions = (
            "# Task: update an existing scheduled job\n\n"
            f"Schedule definitions live at: {cron_path}\n\n"
            f"Current entry for job id='{job_id}':\n"
            f"```json\n{current_job_json}\n```\n\n"
            "Schema fields:\n"
            "  id          short-snake-case-id (do NOT change the id)\n"
            "  name        human-readable name\n"
            "  enabled     true/false\n"
            "  cron_expr   5-field cron expression\n"
            f"  tz          IANA timezone (user's timezone: {tz})\n"
            "  prompt      the full prompt to send when fired — self-contained,\n"
            "              because SOUL/USER/MEMORY context is auto-prepended\n\n"
            "Read the full cron.json file, find the entry with the matching id, "
            "apply only the requested changes, preserve all other fields, and write it back.\n"
            "Confirm with a one-line summary when done.\n\n"
            f"User request:\n{user_request}"
        )
        if prefix:
            return f"{prefix}\n\n---\n\n{task_instructions}"
        return task_instructions

    def _handle_schedule_update(
        self, chat_id: str, job_id: str, user_request: str
    ) -> None:
        """Handle the follow-up message after '!schedule update <id>'."""
        # Load current job JSON to embed in prompt
        current_job_json = "{}"
        cron_path = self._cron_spec_path
        if cron_path and os.path.exists(cron_path):
            try:
                with open(cron_path, encoding="utf-8") as fh:
                    spec_data = json.load(fh)
                entry = next(
                    (e for e in spec_data.get("jobs", []) if e.get("id") == job_id),
                    None,
                )
                if entry:
                    current_job_json = json.dumps(entry, indent=2)
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                logger.warning("Could not read cron.json for update prompt: %s", exc)

        prompt = self._build_schedule_update_prompt(job_id, current_job_json, user_request)

        chat_state = self._db.get_chat(chat_id)
        if chat_state is None:
            chat_state = ChatState(telegram_chat_id=chat_id)

        cwd = chat_state.cwd or self._default_cwd

        sched_job_id = str(uuid.uuid4())
        raw_output_path = os.path.join(self._jobs_dir, f"{sched_job_id}.log")
        job = Job(
            job_id=sched_job_id,
            telegram_chat_id=chat_id,
            session_id=None,
            user_message=f"[schedule-update:{job_id}] {user_request[:80]}",
            status=JobStatus.queued,
        )

        with self._lock:
            if chat_id in self.running_locks:
                self._send(
                    chat_id,
                    "A task is already running. Try !schedule update again when free.",
                )
                return
            self.running_locks[chat_id] = sched_job_id

        try:
            self._db.create_job(job)
        except Exception:
            with self._lock:
                self.running_locks.pop(chat_id, None)
            raise

        thread = threading.Thread(
            target=self._run_scheduled_job_thread,
            args=(job, chat_state, prompt, cwd, raw_output_path),
            daemon=True,
            name=f"schedule-update-{sched_job_id[:8]}",
        )
        thread.start()
        logger.info("Launched schedule-update job %s (chat=%s)", sched_job_id, chat_id)

    def _build_prompt(
        self, user_message: str, mode: str, image_paths: list[str] | None = None
    ) -> str:
        base = user_message

        if image_paths:
            caption = user_message if user_message.strip() else "(no caption)"
            paths_block = "\n".join(f"- {p}" for p in image_paths)
            image_section = (
                f"\n\n## Attached images\n"
                f"The user sent the following image(s). Use the Read tool to view each one "
                f"before replying.\n{paths_block}\n\n"
                f"Caption: {caption}"
            )
            base = image_section

        if self._context_builder is None:
            return base
        try:
            if mode == "new":
                prefix = self._context_builder.build_prefix()
                if prefix:
                    return f"{prefix}\n\n---\n\n# Task\n{base}"
            else:
                hint = self._context_builder.build_resume_hint()
                if hint:
                    return f"{hint}\n\n{base}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Context builder failed, using raw message: %s", exc)
        return base

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

            if not (is_bootstrap and decision.mode == "new"):
                # Suppress only on the first bootstrap turn — "First-time setup..." was
                # already sent. All other turns (normal or bootstrap resume) get the
                # mode/running notification.
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
                prompt = self._build_prompt(job.user_message, decision.mode, job.image_paths)

            # Invoke agent
            agent = self._resolve_agent(chat_state.provider)
            result = agent.run(
                prompt=prompt,
                cwd=cwd,
                session_id=decision.session_id,
                job_id=job.job_id,
                image_paths=job.image_paths,
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
