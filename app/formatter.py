"""
formatter.py — Helpers for building Telegram message strings.
"""

from __future__ import annotations

_TELEGRAM_MAX = 4096


def format_start(task_name: str, mode: str, cwd: str) -> str:
    """Message sent when a job begins."""
    return (
        f"🚀 Starting: {task_name}\n"
        f"Mode: {mode}\n"
        f"Dir: {cwd}"
    )


def format_running() -> str:
    """Message sent while a job is in-flight."""
    return "⏳ Running..."


def format_done(summary: str, exit_code: int, log_path: str) -> str:
    """Message sent when a job finishes successfully."""
    return (
        f"✅ Done\n\n"
        f"{summary}\n\n"
        f"Exit code: {exit_code}\n"
        f"Log: {log_path}"
    )


def format_error(error: str, exit_code: int) -> str:
    """Message sent when a job fails."""
    return (
        f"❌ Failed\n\n"
        f"{error}\n\n"
        f"Exit code: {exit_code}"
    )


def truncate_for_telegram(text: str, max_length: int = 4000) -> str:
    """
    Ensure text fits within Telegram's message size limit.

    Uses max_length (default 4000) rather than the hard limit of 4096
    to leave a small safety margin for surrounding formatting.
    """
    if len(text) <= max_length:
        return text
    suffix = "\n... (truncated)"
    return text[: max_length - len(suffix)] + suffix
