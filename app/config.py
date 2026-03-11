"""
config.py — Load and cache application settings from config/settings.yaml.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List

import yaml

logger = logging.getLogger(__name__)

# Project root is the parent directory of the app/ package.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "config", "settings.yaml")


@dataclass
class Settings:
    telegram_bot_token: str
    allowed_users: List[int] = field(default_factory=list)
    work_dir: str = "/tmp/openlucky_work"
    claude_bin: str = "claude"
    session_timeout_minutes: int = 30
    log_level: str = "INFO"

    @property
    def db_path(self) -> str:
        return os.path.join(_PROJECT_ROOT, "data", "app.db")

    @property
    def jobs_dir(self) -> str:
        return os.path.join(_PROJECT_ROOT, "data", "jobs")

    @property
    def logs_dir(self) -> str:
        return os.path.join(_PROJECT_ROOT, "data", "logs")

    @property
    def project_root(self) -> str:
        return _PROJECT_ROOT


def load() -> Settings:
    """Read settings.yaml and return a Settings instance."""
    with open(_SETTINGS_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    settings = Settings(
        telegram_bot_token=raw.get("telegram_bot_token", ""),
        allowed_users=[int(uid) for uid in raw.get("allowed_users", [])],
        work_dir=raw.get("work_dir", "/tmp/openlucky_work"),
        claude_bin=raw.get("claude_bin", "claude"),
        session_timeout_minutes=int(raw.get("session_timeout_minutes", 30)),
        log_level=raw.get("log_level", "INFO"),
    )

    logger.debug("Settings loaded from %s", _SETTINGS_PATH)
    return settings


@lru_cache(maxsize=1)
def get() -> Settings:
    """Return cached settings, loading them on first call."""
    return load()
