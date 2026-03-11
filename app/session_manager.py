"""
session_manager.py — Decide whether to start a new Claude session or resume an existing one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import ChatState, SessionDecision

logger = logging.getLogger(__name__)

FOLLOWUP_KEYWORDS: list[str] = [
    "继续",
    "刚才",
    "再试",
    "补一下",
    "顺便",
    "continue",
    "fix this too",
    "run again",
    "also",
    "and also",
    "retry",
]

NEW_TASK_KEYWORDS: list[str] = [
    "新任务",
    "another",
    "new task",
    "换个",
    "different",
]


class SessionManager:
    """Determines whether the next job should start a fresh session or resume."""

    def __init__(self, db: Any, timeout_minutes: int = 30) -> None:
        self._db = db
        self._timeout_minutes = timeout_minutes

    def decide(
        self,
        chat_state: ChatState | None,
        text: str,
        force_new: bool = False,
    ) -> SessionDecision:
        """
        Return a SessionDecision based on the current chat state and message text.

        Rules (evaluated in order):
        1. force_new flag → new session
        2. No active_session_id → new session
        3. last_active_at is older than timeout_minutes → new session
        4. message_indicates_new_task(text) → new session
        5. Otherwise → resume
        """
        if force_new:
            logger.debug("decide: force_new=True → new session")
            return SessionDecision(mode="new")

        if chat_state is None or not chat_state.active_session_id:
            logger.debug("decide: no active session → new session")
            return SessionDecision(mode="new")

        if self._is_timed_out(chat_state.last_active_at):
            logger.debug(
                "decide: session %s timed out → new session",
                chat_state.active_session_id,
            )
            return SessionDecision(mode="new")

        if self.message_indicates_new_task(text):
            logger.debug("decide: message looks like a new task → new session")
            return SessionDecision(mode="new")

        logger.debug("decide: resuming session %s", chat_state.active_session_id)
        return SessionDecision(mode="resume", session_id=chat_state.active_session_id)

    def message_indicates_new_task(self, text: str) -> bool:
        """
        Return True when the message contains new-task keywords and does NOT
        contain follow-up keywords (follow-up keywords win over new-task ones).
        """
        lower = text.lower()

        has_new = any(kw.lower() in lower for kw in NEW_TASK_KEYWORDS)
        has_followup = any(kw.lower() in lower for kw in FOLLOWUP_KEYWORDS)

        result = has_new and not has_followup
        if result:
            logger.debug("message_indicates_new_task: True (text=%r)", text[:80])
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_timed_out(self, last_active_at: str | None) -> bool:
        if not last_active_at:
            return True

        try:
            # Handle ISO format strings that may or may not include timezone info
            last = datetime.fromisoformat(last_active_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            elapsed_minutes = (now - last).total_seconds() / 60
            return elapsed_minutes > self._timeout_minutes
        except (ValueError, TypeError) as exc:
            logger.warning("Could not parse last_active_at %r: %s", last_active_at, exc)
            return True
