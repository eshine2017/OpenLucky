"""
command_router.py — Parse and handle Telegram control commands.

Commands are never forwarded to Claude Code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.bootstrap import BootstrapChecker
from app.command_help import TOP_LEVEL_NAMES, render_help
from app.context_builder import ContextBuilder
from app.formatter import truncate_for_telegram
from app.models import ChatState, ChatStatus, JobStatus

logger = logging.getLogger(__name__)

# Derived from the single source of truth in command_help.py
_COMMANDS: frozenset[str] = TOP_LEVEL_NAMES

# "/model" mirrors Claude Code's own slash command so muscle memory still works
# when talking to the bot. Maps recognised slash form -> canonical !command.
_SLASH_ALIASES: dict[str, str] = {"/model": "!model"}

_MODEL_ALIASES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")
_FULL_MODEL_RE = re.compile(r"^(claude|gemini)-[a-z0-9.\-]+$")


class CommandRouter:
    def __init__(
        self,
        db: Any,
        agent: BaseAgent | None = None,
        context_builder: ContextBuilder | None = None,
        bootstrap_checker: BootstrapChecker | None = None,
        scheduler: Any | None = None,
        daemon: Any | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self._db = db
        self._agent = agent
        self._registry = registry
        self._context_builder = context_builder
        self._bootstrap_checker = bootstrap_checker
        self._scheduler = scheduler
        self._daemon = daemon

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_command(self, text: str) -> bool:
        """Return True when text starts with a known !command (or a slash alias)."""
        stripped = text.strip()
        if not stripped:
            return False
        first_word = stripped.split()[0].lower()
        if first_word in _SLASH_ALIASES:
            return True
        if not stripped.startswith("!"):
            return False
        return first_word in _COMMANDS

    def looks_like_command(self, text: str) -> bool:
        """Return True when text starts with '!' (known or unknown), or a slash alias."""
        stripped = text.strip()
        if stripped.startswith("!"):
            return True
        first_word = stripped.split()[0].lower() if stripped else ""
        return first_word in _SLASH_ALIASES

    def handle(self, chat_id: str, text: str) -> str:
        """
        Dispatch the command and return a human-readable response string.

        Parameters
        ----------
        chat_id:  Telegram chat identifier.
        text:     Raw message text (starts with '/').
        """
        parts = text.strip().split(maxsplit=1)
        raw_cmd = parts[0]
        cmd = _SLASH_ALIASES.get(raw_cmd.lower(), raw_cmd.lower())
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
        if cmd == "!provider":
            return self._handle_provider(chat_id, arg)
        if cmd == "!model":
            return self._handle_model(chat_id, arg)
        if cmd == "!schedule":
            return self._handle_schedule(chat_id, arg)
        if cmd == "!help":
            return render_help()

        return render_help(unknown=raw_cmd)

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
            f"Model: {state.model or '(provider default)'}",
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

        if self._registry is not None:
            chat_state = self._db.get_chat(chat_id)
            provider = chat_state.provider if chat_state else None
            self._registry.get(provider).cancel(active_job.job_id)
        elif self._agent is not None:
            self._agent.cancel(active_job.job_id)
        else:
            logger.error("!stop: no agent or registry configured")

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

    def _handle_provider(self, chat_id: str, arg: str) -> str:
        if self._registry is None:
            return "Provider switching not configured."

        state = self._db.get_chat(chat_id)
        current = (state.provider if state else None) or self._registry.default
        available = self._registry.available

        target = arg.strip().lower()
        if not target:
            return (
                f"Current provider: {current}\n"
                f"Available: {', '.join(available)}\n"
                "Usage: !provider <name>"
            )

        if target not in available:
            return f"Unknown provider: {target!r}\nAvailable: {', '.join(available)}"

        active_job = self._db.get_active_job(chat_id)
        if active_job is not None:
            return (
                "A task is currently running. Stop it first with !stop before switching provider."
            )

        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        self._db.upsert_chat(
            replace(
                state,
                provider=target,
                model=None,
                active_session_id=None,
                force_new_next=True,
            )
        )
        return f"Provider switched to {target}. Next message starts a new session."

    def _handle_model(self, chat_id: str, arg: str) -> str:
        state = self._db.get_chat(chat_id)
        current = (state.model if state else None) or "(provider default)"

        target = arg.strip().lower()
        if not target:
            return (
                f"Current model: {current}\n"
                f"Aliases: {', '.join(_MODEL_ALIASES)}\n"
                "Or a full ID (e.g. claude-opus-5, gemini-2.5-pro)\n"
                "Usage: !model <name>"
            )

        if target not in _MODEL_ALIASES and not _FULL_MODEL_RE.match(target):
            return (
                f"Unknown model: {target!r}\n"
                f"Aliases: {', '.join(_MODEL_ALIASES)}, or a full claude-*/gemini-* ID"
            )

        if state is None:
            state = ChatState(telegram_chat_id=chat_id)

        # No active-job guard and no session reset: --model overrides mid-session
        # on --resume, so switching model does not require starting a new session.
        self._db.upsert_chat(replace(state, model=target))
        return f"Model set to {target}. Takes effect on your next message."

    def _handle_schedule(self, chat_id: str, arg: str) -> str:
        if self._scheduler is None:
            return "Scheduler not configured."

        parts = arg.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        subarg = parts[1] if len(parts) > 1 else ""

        if subcmd == "list":
            return self._render_schedule_list(self._scheduler)
        if subcmd == "add":
            return self._handle_schedule_add_cmd(chat_id)
        if subcmd == "run":
            if not subarg:
                return "Usage: !schedule run <id>"
            return self._handle_schedule_run(subarg.strip())
        if subcmd == "remove":
            if not subarg:
                return "Usage: !schedule remove <id>"
            job_id = subarg.strip()
            removed = self._scheduler.remove_job(job_id)
            return f"Job '{job_id}' removed." if removed else f"Job '{job_id}' not found."
        if subcmd == "update":
            if not subarg:
                return "Usage: !schedule update <id>"
            return self._handle_schedule_update_cmd(chat_id, subarg.strip())
        return "Usage: !schedule add | list | run <id> | remove <id> | update <id>"

    def _handle_schedule_add_cmd(self, chat_id: str) -> str:
        if self._daemon is None:
            return "Daemon not connected."
        self._daemon.pending_actions[chat_id] = "schedule_add"
        return (
            "What do you want to schedule? Describe it in plain English\n"
            '(e.g. "daily 8am morning digest of my todos and projects").'
        )

    def _handle_schedule_update_cmd(self, chat_id: str, job_id: str) -> str:
        if self._daemon is None:
            return "Daemon not connected."
        if self._scheduler is None:
            return "Scheduler not configured."
        jobs = self._scheduler.list_jobs()
        if not any(j.id == job_id for j in jobs):
            return f"Job '{job_id}' not found."
        self._daemon.pending_actions[chat_id] = f"schedule_update:{job_id}"
        return (
            f"What do you want to change about '{job_id}'?\n"
            '(e.g. "move to 9am", "change prompt to ...", "disable it")'
        )

    def _handle_schedule_run(self, job_id: str) -> str:
        if self._scheduler is None:
            return "Scheduler not configured."
        loop = getattr(self._scheduler, "_loop", None)
        if loop is None or not loop.is_running():
            return "Scheduler event loop not available."

        future = asyncio.run_coroutine_threadsafe(self._scheduler.run_now(job_id), loop)
        try:
            found = future.result(timeout=10)
        except TimeoutError:
            return "Timeout waiting for job dispatch."
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        return f"Job '{job_id}' dispatched." if found else f"Job '{job_id}' not found."

    def _render_schedule_list(self, scheduler: Any) -> str:
        jobs = scheduler.list_jobs()
        if not jobs:
            return "No scheduled jobs."

        lines: list[str] = []
        for job in jobs:
            status_icon = "✓" if job.enabled else "✗"
            next_run = "(unknown)"
            if job.state.next_run_at_ms is not None:
                dt = datetime.fromtimestamp(job.state.next_run_at_ms / 1000, tz=UTC)
                next_run = dt.strftime("%Y-%m-%d %H:%M UTC")
            tz_label = getattr(job, "tz", "") or "UTC"
            last_status = job.state.last_status or "never run"
            lines.append(
                f"{status_icon} [{job.id}] {job.name}\n"
                f"   schedule: {getattr(job, 'cron_expr', '?')} ({tz_label})\n"
                f"   next: {next_run} | last: {last_status}"
            )
        return "\n\n".join(lines)
