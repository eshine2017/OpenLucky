"""
agents/gemini_code.py — Agent that wraps the Gemini CLI subprocess.

This module knows nothing about Telegram or the database.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

from app.agents.subprocess_agent import SubprocessAgent
from app.models import RunResult

logger = logging.getLogger(__name__)


class GeminiAgent(SubprocessAgent):
    """
    Runs the Gemini CLI as a managed subprocess.

    Usage:
        agent = GeminiAgent(gemini_bin="gemini", work_dir="/tmp/work")
        result = agent.run(prompt="...", cwd="/some/path")
    """

    name = "gemini"

    def __init__(
        self,
        gemini_bin: str,
        work_dir: str,
        gemini_model: str = "",
        workspace_dir: str = "",
        second_brain_dir: str = "",
        images_dir: str = "",
    ) -> None:
        super().__init__(work_dir)
        self.gemini_bin = gemini_bin
        self.gemini_model = gemini_model
        self.workspace_dir = workspace_dir
        self.second_brain_dir = second_brain_dir
        self.images_dir = images_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        job_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> RunResult:
        cmd = self._build_command(prompt, session_id, image_paths=image_paths)

        # Extract the effective session id embedded in the command so we can
        # use it as a fallback when the JSON output lacks one.
        if "--session-id" in cmd:
            fallback_sid = cmd[cmd.index("--session-id") + 1]
        elif "--resume" in cmd:
            fallback_sid = cmd[cmd.index("--resume") + 1]
        else:
            fallback_sid = session_id or ""

        stdout_data, stderr_data, exit_code = self._spawn(cmd, cwd, job_id)
        parsed_session_id, summary = self._parse_output(stdout_data, fallback_sid)

        return RunResult(
            session_id=parsed_session_id,
            stdout=stdout_data,
            stderr=stderr_data,
            exit_code=exit_code,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        image_paths: list[str] | None = None,
    ) -> list[str]:
        cmd = [self.gemini_bin]

        if self.gemini_model:
            cmd += ["-m", self.gemini_model]

        cmd += [
            "--skip-trust",
            "--approval-mode",
            "auto_edit",
            "--output-format",
            "json",
        ]

        dirs: list[str] = []
        if self.workspace_dir and os.path.isdir(self.workspace_dir):
            dirs.append(self.workspace_dir)
        if self.second_brain_dir and os.path.isdir(self.second_brain_dir):
            dirs.append(self.second_brain_dir)
        if image_paths and self.images_dir and os.path.isdir(self.images_dir):
            dirs.append(self.images_dir)

        if dirs:
            cmd += ["--include-directories", ",".join(dirs)]

        if session_id:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", str(uuid.uuid4())]

        cmd += ["-p", prompt]
        return cmd

    def _parse_output(self, stdout: str, fallback_session_id: str) -> tuple[str, str]:
        """Parse Gemini CLI JSON output. Returns (session_id, summary)."""
        try:
            data = json.loads(stdout)
            session_id = data.get("session_id") or fallback_session_id
            summary = self._truncate(data.get("response", "") or stdout)
            return session_id, summary
        except (json.JSONDecodeError, ValueError):
            return fallback_session_id, self._truncate(stdout)
