"""
agents/base.py — Protocol that every agent must satisfy.
"""

from __future__ import annotations

from typing import Protocol

from app.models import RunResult


class BaseAgent(Protocol):
    """Structural interface for all agents."""

    name: str

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

    def cancel(self, job_id: str) -> None:
        """Attempt to cancel a running job."""
        ...
