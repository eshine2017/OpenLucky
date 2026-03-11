"""
command_router.py — Parse and handle Telegram control commands.

Commands are never forwarded to Claude Code.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from app.models import ChatState, ChatStatus, JobStatus

logger = logging.getLogger(__name__)

_COMMANDS = {"/status", "/stop", "/new", "/reset", "/cwd", "/task"}


class CommandRouter:
    def __init__(self, db, session_manager) -> None:
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

    def handle(self, chat_id: str, text: str, runner) -> str:
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
            f"状态: {state.status.value if state.status else 'idle'}",
            f"任务: {state.active_task_name or '(无)'}",
            f"目录: {state.cwd or '(未设置)'}",
            f"会话: {state.active_session_id or '(无)'}",
            f"最后活跃: {state.last_active_at or '(从未)'}",
        ]
        if state.last_summary:
            lines.append(f"\n上次摘要:\n{state.last_summary[:500]}")

        return "\n".join(lines)

    def _handle_stop(self, chat_id: str, runner) -> str:
        active_job = self._db.get_active_job(chat_id)
        if active_job is None:
            return "当前没有正在运行的任务。"

        logger.info("Canceling job %s for chat %s", active_job.job_id, chat_id)

        # Cancel the subprocess
        runner.cancel(active_job.job_id)

        # Update job status
        active_job.status = JobStatus.canceled
        active_job.finished_at = datetime.now(timezone.utc).isoformat()
        self._db.update_job(active_job)

        # Update chat status
        state = self._db.get_chat(chat_id)
        if state:
            state.status = ChatStatus.idle
            self._db.upsert_chat(state)

        return f"已终止任务 {active_job.job_id[:8]}…"

    def _handle_new(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        state.force_new_next = True
        self._db.upsert_chat(state)
        return "下一条消息将开启新会话。"

    def _handle_reset(self, chat_id: str) -> str:
        state = self._db.get_chat(chat_id)
        if state is None:
            return "没有活跃的会话需要重置。"

        old_session = state.active_session_id
        state.active_session_id = None
        self._db.upsert_chat(state)

        if old_session:
            return f"已清除会话绑定 ({old_session[:8]}…)。历史记录已保留。"
        return "没有绑定的会话。"

    def _handle_cwd(self, chat_id: str, path: str) -> str:
        if not path:
            return "用法: /cwd <绝对路径>"

        path = path.strip()
        if not os.path.isabs(path):
            return f"请使用绝对路径。收到: {path!r}"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_cwd = state.cwd
        state.cwd = path
        state.force_new_next = True  # changing cwd forces new session
        self._db.upsert_chat(state)

        msg = f"工作目录已更改: {old_cwd or '(未设置)'} → {path}\n下一条消息将开启新会话。"
        if not os.path.isdir(path):
            msg += f"\n⚠️  警告: 目录 {path!r} 当前不存在。"
        return msg

    def _handle_task(self, chat_id: str, name: str) -> str:
        if not name:
            return "用法: /task <任务名称>"

        state = self._db.get_chat(chat_id)
        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        old_name = state.active_task_name
        state.active_task_name = name.strip()
        self._db.upsert_chat(state)

        return f"任务名称已设置: {old_name or '(无)'} → {state.active_task_name}"
