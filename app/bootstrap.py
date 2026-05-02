"""
bootstrap.py — First-time setup state detection and prompt loading.

Bootstrap runs before any normal conversation when SOUL.md or USER.md are
missing or still at template content. It uses a Claude Code session to
interactively fill those files via Telegram.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from app.models import ChatState

logger = logging.getLogger(__name__)

COMPLETION_SENTINEL = "[[BOOTSTRAP_COMPLETE]]"

_FALLBACK_PROMPT = f"""# First-Time Setup

{{file_status}}

You are helping a new user set up their personal assistant profile.
Two files in the workspace need to be in shape:
- `USER.md` — user profile (name, timezone, language, preferences, role, projects)
- `SOUL.md` — bot identity (name and response style)

Only ask questions about and write to files marked as needing to be filled in
above. Leave files marked "already filled" untouched.
Ask the user a few natural questions (1-2 per turn, conversational tone),
then write the answers into the files using your Edit/Write tools.

When all needed files are updated and the user has confirmed, output the
following sentinel on its own line:

{COMPLETION_SENTINEL}
"""

_ALLOWED_FILES: frozenset[str] = frozenset({"SOUL.md", "USER.md"})


class BootstrapState(StrEnum):
    NEEDED = "needed"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"


@dataclass(frozen=True)
class BootstrapStatus:
    state: BootstrapState
    soul: Literal["missing", "template", "filled"]
    user: Literal["missing", "template", "filled"]
    session_id: str | None  # = chat_state.bootstrap_session_id for daemon use


class BootstrapChecker:
    """Checks bootstrap state and loads the bootstrap prompt."""

    def __init__(self, workspace_dir: str, templates_dir: str) -> None:
        self._workspace_dir = workspace_dir
        self._templates_dir = templates_dir
        self._soul_template = self._load_template_content("SOUL.md")
        self._user_template = self._load_template_content("USER.md")

    def check(self, chat_state: ChatState) -> BootstrapStatus:
        """Return the current bootstrap state based on disk content and chat_state.

        Re-reads files from disk on every call (no caching) so deletion is
        detected immediately on the next incoming message.
        """
        soul_state = self._file_state("SOUL.md", self._soul_template)
        user_state = self._file_state("USER.md", self._user_template)

        if soul_state == "filled" and user_state == "filled":
            return BootstrapStatus(
                state=BootstrapState.COMPLETE,
                soul=soul_state,
                user=user_state,
                session_id=None,
            )

        if chat_state.bootstrap_session_id is not None:
            state = BootstrapState.IN_PROGRESS
        else:
            state = BootstrapState.NEEDED

        return BootstrapStatus(
            state=state,
            soul=soul_state,
            user=user_state,
            session_id=chat_state.bootstrap_session_id,
        )

    def load_bootstrap_prompt(self, status: BootstrapStatus) -> str:
        """Load config/templates/BOOTSTRAP.md and substitute {workspace_dir} and {file_status}.

        Uses str.replace (not str.format) to avoid KeyError if the path contains
        brace characters. Falls back to a hardcoded minimal prompt if the file
        is missing.
        """
        path = os.path.join(self._templates_dir, "BOOTSTRAP.md")
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            logger.warning("BOOTSTRAP.md not found at %s; using fallback prompt", path)
            content = _FALLBACK_PROMPT

        file_status_block = self._render_file_status(status)
        return (
            content
            .replace("{workspace_dir}", self._workspace_dir)
            .replace("{file_status}", file_status_block)
        )

    def _render_file_status(self, status: BootstrapStatus) -> str:
        label: dict[str, str] = {
            "filled":   "already filled — DO NOT modify or re-ask its questions",
            "template": "still at template content — needs to be filled in",
            "missing":  "missing — needs to be created and filled in",
        }
        return (
            "Current file state:\n"
            f"- USER.md: {label[status.user]}\n"
            f"- SOUL.md: {label[status.soul]}"
        )

    def _file_state(
        self,
        filename: str,
        template: str,
    ) -> Literal["missing", "template", "filled"]:
        """Three-way check: missing | template | filled. Returns 'missing' on OSError.

        Only 'SOUL.md' and 'USER.md' are accepted to prevent path-traversal.
        """
        if filename not in _ALLOWED_FILES:
            allowed = sorted(_ALLOWED_FILES)
            raise ValueError(f"_file_state only accepts {allowed!r}, got {filename!r}")
        path = os.path.join(self._workspace_dir, filename)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return "missing"

        if not content.strip():
            return "missing"

        if template and content.strip() == template.strip():
            return "template"

        return "filled"

    def _load_template_content(self, filename: str) -> str:
        path = os.path.join(self._templates_dir, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            logger.warning("Template not found: %s", path)
            return ""


def is_complete_signal(stdout: str, summary: str) -> bool:
    """True if the bootstrap completion sentinel appears in stdout or summary.

    Note: a user typing the sentinel string into Telegram cannot cause false
    completion because BootstrapChecker.check() file-content verification gates
    the actual state transition.
    """
    return COMPLETION_SENTINEL in stdout or COMPLETION_SENTINEL in summary
