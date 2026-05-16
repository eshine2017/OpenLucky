"""
agents/subprocess_agent.py — Shared subprocess base for all agent implementations.

Provides: process registry, cancel (SIGTERM → SIGKILL), _spawn(), and _truncate().
Subclasses must implement _build_command() and run().
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod

from app.models import RunResult

logger = logging.getLogger(__name__)

_SUMMARY_MAX = 3000


class SubprocessAgent(ABC):
    """Abstract base that manages subprocess lifecycle for agent implementations."""

    name: str

    def __init__(self, work_dir: str) -> None:
        self.work_dir = work_dir
        # job_id → Popen; guarded by _proc_lock
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        image_paths: list[str] | None = None,
    ) -> list[str]:
        """Return the full argv for the subprocess."""
        ...

    @abstractmethod
    def run(
        self,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        job_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> RunResult:
        """Execute a prompt and return a RunResult."""
        ...

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def cancel(self, job_id: str) -> None:
        """Send SIGTERM to the process registered under job_id.
        Falls back to SIGKILL after 5 seconds if the process does not exit."""
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
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGKILL)

    def _spawn(
        self,
        cmd: list[str],
        cwd: str,
        job_id: str | None,
    ) -> tuple[str, str, int]:
        """Start the subprocess and wait for it to complete.

        Returns (stdout, stderr, exit_code).
        """
        effective_cwd = cwd if os.path.isdir(cwd) else self.work_dir
        os.makedirs(effective_cwd, exist_ok=True)

        logger.info("Spawning %s: %s (cwd=%s)", self.name, " ".join(cmd), effective_cwd)

        proc = subprocess.Popen(
            cmd,
            cwd=effective_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        _key = job_id or str(proc.pid)
        with self._proc_lock:
            self._processes[_key] = proc

        try:
            stdout_data, stderr_data = proc.communicate()
        finally:
            with self._proc_lock:
                self._processes.pop(_key, None)

        exit_code = proc.returncode
        logger.info("%s exited with code %d (job=%s)", self.name, exit_code, _key)
        return stdout_data, stderr_data, exit_code

    def _truncate(self, text: str) -> str:
        """Truncate text to _SUMMARY_MAX characters, appending a marker if cut."""
        if len(text) > _SUMMARY_MAX:
            return text[:_SUMMARY_MAX] + "\n… (truncated)"
        return text
