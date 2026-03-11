"""
formatter.py — Helpers for building Telegram message strings.
"""

from __future__ import annotations

_TELEGRAM_MAX = 4096


def format_start(task_name: str, mode: str, cwd: str) -> str:
    """Message sent when a job begins."""
    return (
        f"🚀 开始处理: {task_name}\n"
        f"模式: {mode}\n"
        f"目录: {cwd}"
    )


def format_running() -> str:
    """Message sent while a job is in-flight."""
    return "⏳ 正在执行中..."


def format_done(summary: str, exit_code: int, log_path: str) -> str:
    """Message sent when a job finishes successfully."""
    return (
        f"✅ 已完成\n\n"
        f"{summary}\n\n"
        f"退出码: {exit_code}\n"
        f"日志: {log_path}"
    )


def format_error(error: str, exit_code: int) -> str:
    """Message sent when a job fails."""
    return (
        f"❌ 执行失败\n\n"
        f"{error}\n\n"
        f"退出码: {exit_code}"
    )


def truncate_for_telegram(text: str, max_length: int = 4000) -> str:
    """
    Ensure text fits within Telegram's message size limit.

    Uses max_length (default 4000) rather than the hard limit of 4096
    to leave a small safety margin for surrounding formatting.
    """
    if len(text) <= max_length:
        return text
    suffix = "\n… (内容已截断)"
    return text[: max_length - len(suffix)] + suffix
