"""
claude_runner.py — Spawn Claude Code as a subprocess, collect output, parse results.

This module knows nothing about Telegram or the database.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional

from app.models import RunResult

logger = logging.getLogger(__name__)


class ClaudeRunner:
    """
    Wraps the Claude Code CLI.  One instance is shared across the daemon.

    Usage:
        runner = ClaudeRunner(claude_bin="claude", work_dir="/tmp/openlucky_work")
        result = runner.run(prompt="...", cwd="/some/path", session_id=None)
    """

    def __init__(self, claude_bin: str, work_dir: str) -> None:
        self.claude_bin = claude_bin
        self.work_dir = work_dir
        # job_id → Popen; guarded by _proc_lock
        self._processes: dict[str, subprocess.Popen] = {}
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        cwd: str,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> RunResult:
        """
        Run Claude Code with the given prompt.

        Parameters
        ----------
        prompt:     The user message / instruction.
        cwd:        Working directory for the subprocess.
        session_id: If set, pass --resume <session_id>.
        job_id:     Optional key used to track the process for cancellation.

        Returns
        -------
        RunResult with parsed session_id, stdout, stderr, exit_code and summary.
        """
        cmd = self._build_command(prompt, session_id)
        effective_cwd = cwd if os.path.isdir(cwd) else self.work_dir
        os.makedirs(effective_cwd, exist_ok=True)

        logger.info(
            "Spawning Claude Code: %s (cwd=%s, session=%s)",
            " ".join(cmd),
            effective_cwd,
            session_id,
        )

        proc = subprocess.Popen(
            cmd,
            cwd=effective_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Register process so it can be cancelled
        _key = job_id or str(proc.pid)
        with self._proc_lock:
            self._processes[_key] = proc

        try:
            stdout_data, stderr_data = proc.communicate()
        finally:
            with self._proc_lock:
                self._processes.pop(_key, None)

        exit_code = proc.returncode
        logger.info("Claude Code exited with code %d (job=%s)", exit_code, _key)

        parsed_session_id, summary = self._parse_stream_json(stdout_data)

        return RunResult(
            session_id=parsed_session_id or session_id or "",
            stdout=stdout_data,
            stderr=stderr_data,
            exit_code=exit_code,
            summary=summary,
        )

    def cancel(self, job_id: str) -> None:
        """
        Send SIGTERM to the process registered under job_id.
        If it does not exit within 5 seconds, send SIGKILL.
        """
        with self._proc_lock:
            proc = self._processes.get(job_id)

        if proc is None:
            logger.warning("cancel(%s): no active process found", job_id)
            return

        logger.info("Sending SIGTERM to process %d (job=%s)", proc.pid, job_id)
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                logger.info("Process %d terminated gracefully", proc.pid)
                return
            time.sleep(0.2)

        logger.warning("Process %d did not exit; sending SIGKILL", proc.pid)
        try:
            proc.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_command(self, prompt: str, session_id: Optional[str]) -> list[str]:
        cmd = [
            self.claude_bin,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
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
                # Primary source of session_id and final summary
                if "session_id" in obj:
                    parsed_session_id = obj["session_id"]
                result_text = obj.get("result", "")
                if result_text:
                    summary_parts.append(result_text)

            elif event_type == "assistant":
                # Accumulate assistant message content for fallback summary
                message = obj.get("message", {})
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_text_parts.append(block.get("text", ""))

        if summary_parts:
            summary = "\n".join(summary_parts)
        elif assistant_text_parts:
            # Fallback: use the last assistant text block
            summary = assistant_text_parts[-1]
        else:
            summary = "(No summary available)"

        # Trim summary to a reasonable length for Telegram
        if len(summary) > 3000:
            summary = summary[:3000] + "\n… (truncated)"

        return parsed_session_id, summary
