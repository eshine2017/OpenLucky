"""
config.py — Load and cache application settings.

Config file is resolved in this order:
  1. CONFIG_FILE env var (absolute or relative to project root)
  2. config/settings.yaml (default)

Example:
    CONFIG_FILE=config/settings.dev.yaml python3 -m app.main
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


def _resolve_config_path() -> str:
    env = os.environ.get("CONFIG_FILE")
    if env:
        path = env if os.path.isabs(env) else os.path.join(_PROJECT_ROOT, env)
        return path
    return os.path.join(_PROJECT_ROOT, "config", "settings.yaml")


@dataclass
class Settings:
    telegram_bot_token: str
    allowed_users: List[int] = field(default_factory=list)
    work_dir: str = "/tmp/openlucky_work"
    claude_bin: str = "claude"
    session_timeout_minutes: int = 30
    log_level: str = "INFO"
    # data_dir: where DB, jobs/, logs/ live. Defaults to data/ in project root.
    # Set this in settings.yaml to separate prod and dev data.
    data_dir: str = ""

    @property
    def _effective_data_dir(self) -> str:
        return self.data_dir if self.data_dir else os.path.join(_PROJECT_ROOT, "data")

    @property
    def db_path(self) -> str:
        return os.path.join(self._effective_data_dir, "app.db")

    @property
    def jobs_dir(self) -> str:
        return os.path.join(self._effective_data_dir, "jobs")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self._effective_data_dir, "logs")

    @property
    def project_root(self) -> str:
        return _PROJECT_ROOT


def load() -> Settings:
    """Read the config file and return a Settings instance."""
    config_path = _resolve_config_path()
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    settings = Settings(
        telegram_bot_token=raw.get("telegram_bot_token", ""),
        allowed_users=[int(uid) for uid in raw.get("allowed_users", [])],
        work_dir=raw.get("work_dir", "/tmp/openlucky_work"),
        claude_bin=raw.get("claude_bin", "claude"),
        session_timeout_minutes=int(raw.get("session_timeout_minutes", 30)),
        log_level=raw.get("log_level", "INFO"),
        data_dir=raw.get("data_dir", ""),
    )

    logger.debug("Settings loaded from %s", config_path)
    return settings


@lru_cache(maxsize=1)
def get() -> Settings:
    """Return cached settings, loading them on first call."""
    return load()
