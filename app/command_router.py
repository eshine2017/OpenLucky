"""
command_router.py — Parse and handle Telegram control commands.

Commands are never forwarded to Claude Code.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.agents.base import BaseAgent
from app.bootstrap import BootstrapChecker
from app.context_builder import ContextBuilder
from app.formatter import truncate_for_telegram
from app.models import ChatState, ChatStatus, JobStatus

logger = logging.getLogger(__name__)

_COMMANDS = {"!status", "!stop", "!new", "!reset", "!cwd", "!task", "!soul", "!whoami", "!memory"}


class CommandRouter:
    def __init__(
        self,
        db: Any,
        agent: BaseAgent,
        context_builder: ContextBuilder | None = None,
        bootstrap_checker: BootstrapChecker | None = None,
    ) -> None:
        self._db = db
        self._agent = agent
        self._context_builder = context_builder
        self._bootstrap_checker = bootstrap_checker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_command(self, text: str) -> bool:
        """Return True when text starts with a known !command."""
        if not text.strip().startswith("!"):
            return False
        first_word = text.strip().split()[0].lower()
        return first_word in _COMMANDS

    def looks_like_command(self, text: str) -> bool:
        """Return True when text starts with '!' (known or unknown command)."""
        return text.strip().startswith("!")

    def handle(self, chat_id: str, text: str) -> str:
        """
        Dispatch the command and return a human-readable response string.

        Parameters
        ----------
        chat_id:  Telegram chat identifier.
        text:     Raw message text (starts with '/').
        """
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        logger.info("Command %r from chat %s (arg=%r)", cmd, chat_id, arg)

        if cmd == "!status":
            return self._handle_status(chat_id)
        if cmd == "!stop":
            return self._handle_stop(chat_id)
        if cmd == "!new":
            return self._handle_new(chat_id)
        if cmd == "!reset":
            return self._handle_reset(chat_id)
        if cmd == "!cwd":
            return self._handle_cwd(chat_id, arg)
        if cmd == "!task":
            return self._handle_task(chat_id, arg)
        if cmd == "!soul":
            return self._handle_soul()
        if cmd == "!whoami":
            return self._handle_whoami()
        if cmd == "!memory":
            return self._handle_memory()

        return (
            "Unknown command. Available: !status !stop !new !reset !cwd !task !soul !whoami !memory"  # noqa: E501
        )

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

        if self._bootstrap_checker is not None:
            bs = self._bootstrap_checker.check(state)
            lines.append(f"Bootstrap: {bs.state.value}")
            lines.append(f"Files: SOUL.md={bs.soul} USER.md={bs.user}")

        if state.last_summary:
            lines.append(f"\nLast summary:\n{state.last_summary[:500]}")

        return "\n".join(lines)

    def _handle_stop(self, chat_id: str) -> str:
        active_job = self._db.get_active_job(chat_id)
        if active_job is None:
            return "No task is currently running."

        logger.info("Canceling job %s for chat %s", active_job.job_id, chat_id)

        self._agent.cancel(active_job.job_id)

        # Update job status
        active_job = replace(
            active_job,
            status=JobStatus.canceled,
            finished_at=datetime.now(UTC).isoformat(),
        )
        self._db.update_job(active_job)

        # Update chat status
        state = self._db.get_chat(chat_id)
        if state:
            self._db.upsert_chat(replace(state, status=ChatStatus.idle))

        return f"Canceled job {active_job.job_id[:8]}..."

    def _handle_new(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        self._db.upsert_chat(replace(state, force_new_next=True))
        return "Next message will start a new session."

    def _handle_reset(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            return "No active session to reset."

        old_session = state.active_session_id
        old_bootstrap = state.bootstrap_session_id
        self._db.upsert_chat(replace(state, active_session_id=None, bootstrap_session_id=None))

        if old_session or old_bootstrap:
            parts = []
            if old_session:
                parts.append(f"session ({old_session[:8]}...)")
            if old_bootstrap:
                parts.append(f"bootstrap session ({old_bootstrap[:8]}...)")
            return f"Cleared: {', '.join(parts)}. History preserved."
        return "No session was bound."

    def _handle_cwd(self, chat_id: str, path: str) -> str:
        if not path:
            return "Usage: !cwd <absolute path>"

        path = path.strip()
        if not os.path.isabs(path):
            return f"Please use an absolute path. Got: {path!r}"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_cwd = state.cwd
        self._db.upsert_chat(replace(state, cwd=path, force_new_next=True))

        msg = (
            f"Working dir changed: {old_cwd or '(not set)'} -> {path}\n"
            "Next message will start a new session."
        )
        if not os.path.isdir(path):
            msg += f"\n⚠️  Warning: {path!r} does not exist."
        return msg

    def _handle_task(self, chat_id: str, name: str) -> str:
        if not name:
            return "Usage: !task <name>"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_name = state.active_task_name
        new_name = name.strip()
        self._db.upsert_chat(replace(state, active_task_name=new_name))

        return f"Task name set: {old_name or '(none)'} -> {new_name}"

    def _handle_soul(self) -> str:
        if self._context_builder is None:
            return "Memory feature not configured."
        content = self._context_builder.read_soul().strip()
        if not content:
            return "(SOUL.md is empty or matches the default template)"
        return truncate_for_telegram(content)

    def _handle_whoami(self) -> str:
        if self._context_builder is None:
            return "Memory feature not configured."
        content = self._context_builder.read_user().strip()
        if not content:
            return "(USER.md is empty or matches the default template)"
        return truncate_for_telegram(content)

    def _handle_memory(self) -> str:
        if self._context_builder is None:
            return "Memory feature not configured."
        content = self._context_builder.read_memory().strip()
        if not content:
            return "(memory/MEMORY.md is empty or matches the default template)"
        return truncate_for_telegram(content)
