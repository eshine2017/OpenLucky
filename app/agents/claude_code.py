"""
agents/claude_code.py — Agent that wraps the Claude Code CLI subprocess.

This module knows nothing about Telegram or the database.
"""

from __future__ import annotations

import json
import logging
import os

from app.agents.subprocess_agent import SubprocessAgent
from app.models import RunResult

logger = logging.getLogger(__name__)


class ClaudeCodeAgent(SubprocessAgent):
    """
    Runs Claude Code as a managed subprocess.

    Usage:
        agent = ClaudeCodeAgent(claude_bin="/path/to/claude", work_dir="/tmp/work")
        result = agent.run(prompt="...", cwd="/some/path")
    """

    name = "claude"

    def __init__(
        self,
        claude_bin: str,
        work_dir: str,
        workspace_dir: str = "",
        second_brain_dir: str = "",
        images_dir: str = "",
    ) -> None:
        super().__init__(work_dir)
        self.claude_bin = claude_bin
        self.workspace_dir = workspace_dir
        self.second_brain_dir = second_brain_dir
        self.images_dir = images_dir
        if second_brain_dir and not os.path.isdir(second_brain_dir):
            logger.warning(
                "second_brain_dir %r does not exist; --add-dir will be skipped until it is created",
                second_brain_dir,
            )

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
        stdout_data, stderr_data, exit_code = self._spawn(cmd, cwd, job_id)
        parsed_session_id, summary = self._parse_stream_json(stdout_data)
        return RunResult(
            session_id=parsed_session_id or session_id or "",
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
        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
        ]
        if self.workspace_dir:
            cmd += ["--add-dir", self.workspace_dir]
        if self.second_brain_dir and os.path.isdir(self.second_brain_dir):
            cmd += ["--add-dir", self.second_brain_dir]
        if image_paths and self.images_dir:
            cmd += ["--add-dir", self.images_dir]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def _parse_stream_json(self, raw_output: str) -> tuple[str | None, str]:
        """
        Parse newline-delimited JSON from Claude Code's stream-json output.

        Looks for a JSON object with "type": "result" which contains:
          - "session_id": the session identifier
          - "result": the final text summary

        Returns (session_id | None, summary_text).
        """
        parsed_session_id: str | None = None
        summary_parts: list[str] = []
        assistant_text_parts: list[str] = []

        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = obj.get("type", "")

            if event_type == "result":
                if "session_id" in obj:
                    parsed_session_id = obj["session_id"]
                result_text = obj.get("result", "")
                if result_text:
                    summary_parts.append(result_text)

            elif event_type == "assistant":
                message = obj.get("message", {})
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_text_parts.append(block.get("text", ""))

        if summary_parts:
            summary = "\n".join(summary_parts)
        elif assistant_text_parts:
            summary = assistant_text_parts[-1]
        else:
            summary = "(No summary available)"

        return parsed_session_id, self._truncate(summary)
