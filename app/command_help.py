"""command_help.py — Command metadata and help text renderer."""

from __future__ import annotations

from dataclasses import dataclass

# Characters reserved for the usage/command column before summary aligns
_USAGE_COL_WIDTH = 28
_CATEGORY_ORDER = ("info", "session", "provider", "schedule")
_CATEGORY_LABELS = {
    "info": "Info",
    "session": "Session",
    "provider": "Provider",
    "schedule": "Schedule",
}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    category: str


COMMANDS: tuple[CommandSpec, ...] = (
    # Info
    CommandSpec("!status", "!status", "Show current session status, task, dir, last summary", "info"),  # noqa: E501
    CommandSpec("!soul", "!soul", "Show bot identity (SOUL.md)", "info"),
    CommandSpec("!whoami", "!whoami", "Show user profile (USER.md)", "info"),
    CommandSpec("!memory", "!memory", "Show long-term memory (MEMORY.md)", "info"),
    CommandSpec("!help", "!help", "Show this help message", "info"),
    # Session
    CommandSpec("!new", "!new", "Force next message to start a new session", "session"),
    CommandSpec("!reset", "!reset", "Clear active session binding (history kept)", "session"),
    CommandSpec("!stop", "!stop", "Cancel the currently running task", "session"),
    CommandSpec("!cwd", "!cwd <path>", "Switch working directory (forces new session)", "session"),
    CommandSpec("!task", "!task <name>", "Set active task name", "session"),
    # Provider
    CommandSpec(
        "!provider",
        "!provider [name]",
        "View or switch AI provider (claude/gemini); starts new session",
        "provider",
    ),
    # Schedule — parent entry used only for routing; sub-commands shown in help
    CommandSpec("!schedule", "!schedule <subcmd>", "Manage cron jobs", "schedule"),
)

# Top-level command names for routing derivation (sub-commands are display-only)
TOP_LEVEL_NAMES: frozenset[str] = frozenset(spec.name for spec in COMMANDS)

_SCHEDULE_SUBCMDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "!schedule list",
        "!schedule list",
        "List all cron jobs with next-run time and last status",
        "schedule",
    ),
    CommandSpec(
        "!schedule add",
        "!schedule add",
        "Start interactive flow to create a new cron job",
        "schedule",
    ),
    CommandSpec(
        "!schedule run",
        "!schedule run <id>",
        "Trigger a cron job immediately (off-schedule)",
        "schedule",
    ),
    CommandSpec(
        "!schedule remove",
        "!schedule remove <id>",
        "Delete a cron job",
        "schedule",
    ),
    CommandSpec(
        "!schedule update",
        "!schedule update <id>",
        "Start interactive flow to modify an existing cron job",
        "schedule",
    ),
)


def _fmt_line(usage: str, summary: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}{usage.ljust(_USAGE_COL_WIDTH)} {summary}"


def render_help(unknown: str | None = None) -> str:
    """Return formatted help text, optionally prefixed with an unknown-command notice."""
    lines: list[str] = []

    if unknown is not None:
        lines.append(f"Unknown command: {unknown}")
        lines.append("")

    lines.append("Available commands:")

    for cat in _CATEGORY_ORDER:
        lines.append(f"\n[{_CATEGORY_LABELS[cat]}]")
        if cat == "schedule":
            for sub in _SCHEDULE_SUBCMDS:
                lines.append(_fmt_line(sub.usage, sub.summary))
            continue
        for spec in COMMANDS:
            if spec.category == cat:
                lines.append(_fmt_line(spec.usage, spec.summary))

    return "\n".join(lines)
