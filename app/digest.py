"""
digest.py — Morning digest prompt builder.

Reads second_brain_dir/inbox/now.md and projects/*/README.md to construct
a prompt for Claude summarising today's priorities.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_NOW_MD_CAP = 16 * 1024  # 16 KB
_README_CAP = 4 * 1024   # 4 KB per project README
_MAX_PROJECTS = 10


def build_morning_digest_prompt(second_brain_dir: str) -> str | None:
    """
    Build a Claude prompt for a morning digest.

    Returns None when second_brain_dir is empty/missing or no source files
    are found (caller should record skipped status).
    """
    if not second_brain_dir or not os.path.isdir(second_brain_dir):
        return None

    sections: list[str] = []

    # --- inbox/now.md ---
    now_md_path = os.path.join(second_brain_dir, "inbox", "now.md")
    if os.path.isfile(now_md_path):
        try:
            with open(now_md_path, encoding="utf-8") as fh:
                content = fh.read(_NOW_MD_CAP)
            if content.strip():
                sections.append(f"## Current tasks (inbox/now.md)\n\n{content}")
        except OSError as exc:
            logger.warning("Could not read %s: %s", now_md_path, exc)

    # --- projects/*/README.md (sorted by mtime descending, first 10) ---
    projects_dir = os.path.join(second_brain_dir, "projects")
    if os.path.isdir(projects_dir):
        readmes: list[tuple[float, str]] = []
        try:
            for entry in os.scandir(projects_dir):
                if not entry.is_dir():
                    continue
                readme = os.path.join(entry.path, "README.md")
                if os.path.isfile(readme):
                    mtime = os.path.getmtime(readme)
                    readmes.append((mtime, readme))
        except OSError as exc:
            logger.warning("Could not scan %s: %s", projects_dir, exc)

        readmes.sort(key=lambda t: t[0], reverse=True)
        for _, readme_path in readmes[:_MAX_PROJECTS]:
            try:
                with open(readme_path, encoding="utf-8") as fh:
                    content = fh.read(_README_CAP)
                if content.strip():
                    proj_name = os.path.basename(os.path.dirname(readme_path))
                    sections.append(f"## Project: {proj_name}\n\n{content}")
            except OSError as exc:
                logger.warning("Could not read %s: %s", readme_path, exc)

    if not sections:
        return None

    sources = "\n\n---\n\n".join(sections)
    return (
        "You are preparing a morning digest. Below is context from the user's second brain.\n\n"
        "Summarise today's must-do items and anything due in the next 3 days.\n"
        "Format: bullet list, ≤ 300 words, no preamble.\n"
        "Be direct; if nothing is due, say so in one line.\n\n"
        "---\n\n"
        f"{sources}"
    )


def read_user_timezone(workspace_dir: str) -> str | None:
    """
    Read the user's IANA timezone from workspace_dir/USER.md.

    Looks for a line like:  timezone: America/Los_Angeles
    Returns the timezone string if valid, else None.
    """
    user_md = os.path.join(workspace_dir, "USER.md")
    if not os.path.isfile(user_md):
        return None

    try:
        with open(user_md, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*timezone\s*:\s*(.+)$", line, re.IGNORECASE)
                if m:
                    tz_value = m.group(1).strip()
                    try:
                        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

                        ZoneInfo(tz_value)
                        return tz_value
                    except (ZoneInfoNotFoundError, KeyError):
                        logger.warning("Invalid timezone in USER.md: %r", tz_value)
                        return None
    except OSError as exc:
        logger.warning("Could not read USER.md: %s", exc)

    return None
