"""
models.py — Dataclasses for openlucky domain objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ChatStatus(StrEnum):
    idle = "idle"
    running = "running"
    error = "error"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    canceled = "canceled"


@dataclass
class ChatState:
    telegram_chat_id: str
    active_session_id: str | None = None
    active_task_name: str | None = None
    cwd: str | None = None
    status: ChatStatus = ChatStatus.idle
    last_active_at: str | None = None
    last_summary: str | None = None
    force_new_next: bool = False


@dataclass
class Job:
    job_id: str
    telegram_chat_id: str
    session_id: str | None = None
    user_message: str = ""
    status: JobStatus = JobStatus.queued
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    result_summary: str | None = None
    raw_output_path: str | None = None


@dataclass
class RunResult:
    session_id: str
    stdout: str
    stderr: str
    exit_code: int
    summary: str


@dataclass
class SessionDecision:
    mode: Literal["new", "resume"]
    session_id: str | None = None
