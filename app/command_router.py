"""
command_router.py — Parse and handle Telegram control commands.

Commands are never forwarded to Claude Code.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.models import ChatState, ChatStatus, JobStatus

logger = logging.getLogger(__name__)

_COMMANDS = {"/status", "/stop", "/new", "/reset", "/cwd", "/task"}


class CommandRouter:
    def __init__(self, db: Any, session_manager: Any) -> None:
        self._db = db
        self._session_manager = session_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_command(self, text: str) -> bool:
        """Return True when text starts with a known /command."""
        if not text.startswith("/"):
            return False
        first_word = text.split()[0].lower()
        return first_word in _COMMANDS

    def handle(self, chat_id: str, text: str, runner: Any) -> str:
        """
        Dispatch the command and return a human-readable response string.

        Parameters
        ----------
        chat_id:  Telegram chat identifier.
        text:     Raw message text (starts with '/').
        runner:   ClaudeRunner instance (needed by /stop).
        """
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        logger.info("Command %r from chat %s (arg=%r)", cmd, chat_id, arg)

        if cmd == "/status":
            return self._handle_status(chat_id)
        if cmd == "/stop":
            return self._handle_stop(chat_id, runner)
        if cmd == "/new":
            return self._handle_new(chat_id)
        if cmd == "/reset":
            return self._handle_reset(chat_id)
        if cmd == "/cwd":
            return self._handle_cwd(chat_id, arg)
        if cmd == "/task":
            return self._handle_task(chat_id, arg)

        return "Unknown command. Available: /status /stop /new /reset /cwd /task"

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_status(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            return "No active session. Send a message to start."

        lines = [
            f"Status: {state.status.value if state.status else 'idle'}",
            f"Task: {state.active_task_name or '(none)'}",
            f"Dir: {state.cwd or '(not set)'}",
            f"Session: {state.active_session_id or '(none)'}",
            f"Last active: {state.last_active_at or '(never)'}",
        ]
        if state.last_summary:
            lines.append(f"\nLast summary:\n{state.last_summary[:500]}")

        return "\n".join(lines)

    def _handle_stop(self, chat_id: str, runner: Any) -> str:
        active_job = self._db.get_active_job(chat_id)
        if active_job is None:
            return "No task is currently running."

        logger.info("Canceling job %s for chat %s", active_job.job_id, chat_id)

        # Cancel the subprocess
        runner.cancel(active_job.job_id)

        # Update job status
        active_job.status = JobStatus.canceled
        active_job.finished_at = datetime.now(UTC).isoformat()
        self._db.update_job(active_job)

        # Update chat status
        state = self._db.get_chat(chat_id)
        if state:
            state.status = ChatStatus.idle
            self._db.upsert_chat(state)

        return f"Canceled job {active_job.job_id[:8]}..."

    def _handle_new(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        state.force_new_next = True
        self._db.upsert_chat(state)
        return "Next message will start a new session."

    def _handle_reset(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            return "No active session to reset."

        old_session = state.active_session_id
        state.active_session_id = None
        self._db.upsert_chat(state)

        if old_session:
            return f"Session cleared ({old_session[:8]}...). History preserved."
        return "No session was bound."

    def _handle_cwd(self, chat_id: str, path: str) -> str:
        if not path:
            return "Usage: /cwd <absolute path>"

        path = path.strip()
        if not os.path.isabs(path):
            return f"Please use an absolute path. Got: {path!r}"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_cwd = state.cwd
        state.cwd = path
        state.force_new_next = True  # changing cwd forces new session
        self._db.upsert_chat(state)

        msg = (
            f"Working dir changed: {old_cwd or '(not set)'} -> {path}\n"
            "Next message will start a new session."
        )
        if not os.path.isdir(path):
            msg += f"\n⚠️  Warning: {path!r} does not exist."
        return msg

    def _handle_task(self, chat_id: str, name: str) -> str:
        if not name:
            return "Usage: /task <name>"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_name = state.active_task_name
        state.active_task_name = name.strip()
        self._db.upsert_chat(state)

        return f"Task name set: {old_name or '(none)'} -> {state.active_task_name}"
