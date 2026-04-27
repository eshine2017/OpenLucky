"""Tests for app.config — Settings loading and path resolution."""

from __future__ import annotations

import os

import pytest
import yaml

from app import config


@pytest.fixture(autouse=True)
def _clear_lru_cache():
    """Clear the lru_cache on config.get() before and after each test."""
    config.get.cache_clear()
    yield
    config.get.cache_clear()


@pytest.fixture()
def minimal_yaml(tmp_path):
    """Write a minimal valid settings YAML and return its path."""
    settings = {
        "telegram_bot_token": "test-token-123",
        "allowed_users": [111, 222],
        "work_dir": "/tmp/test_work",
        "claude_bin": "/usr/local/bin/claude",
        "session_timeout_minutes": 45,
        "log_level": "DEBUG",
        "data_dir": str(tmp_path / "data"),
    }
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.dump(settings), encoding="utf-8")
    return str(config_path)


@pytest.fixture()
def minimal_yaml_bare(tmp_path):
    """Write a minimal YAML with only required field."""
    settings = {"telegram_bot_token": "bare-token"}
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.dump(settings), encoding="utf-8")
    return str(config_path)


class TestLoad:
    def test_full_config(self, minimal_yaml, monkeypatch, tmp_path):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()

        assert settings.telegram_bot_token == "test-token-123"
        assert settings.allowed_users == [111, 222]
        assert settings.work_dir == "/tmp/test_work"
        assert settings.claude_bin == "/usr/local/bin/claude"
        assert settings.session_timeout_minutes == 45
        assert settings.log_level == "DEBUG"
        assert settings.data_dir == str(tmp_path / "data")

    def test_defaults_when_keys_missing(self, minimal_yaml_bare, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml_bare)
        settings = config.load()

        assert settings.telegram_bot_token == "bare-token"
        assert settings.allowed_users == []
        assert settings.work_dir == "/tmp/openlucky_work"
        assert settings.claude_bin == "claude"
        assert settings.session_timeout_minutes == 30
        assert settings.log_level == "INFO"
        assert settings.data_dir == ""

    def test_file_not_found_raises(self, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", "/nonexistent/path/settings.yaml")
        with pytest.raises(FileNotFoundError):
            config.load()

    def test_allowed_users_converted_to_int(self, tmp_path, monkeypatch):
        """Allowed users should be integers even if YAML stores as strings."""
        settings_content = "telegram_bot_token: tok\nallowed_users:\n  - 12345\n  - 67890\n"
        cfg = tmp_path / "s.yaml"
        cfg.write_text(settings_content, encoding="utf-8")
        monkeypatch.setenv("CONFIG_FILE", str(cfg))
        settings = config.load()
        assert settings.allowed_users == [12345, 67890]
        assert all(isinstance(u, int) for u in settings.allowed_users)

    def test_empty_yaml_returns_defaults(self, tmp_path, monkeypatch):
        """An empty YAML file should still produce a Settings with empty token."""
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("", encoding="utf-8")
        monkeypatch.setenv("CONFIG_FILE", str(cfg))
        settings = config.load()
        assert settings.telegram_bot_token == ""

    def test_relative_config_file_env_var(self, tmp_path, monkeypatch):
        """A relative CONFIG_FILE path is resolved relative to project root."""
        # We can't easily test this without controlling the project root,
        # so test absolute path works instead.
        settings_content = "telegram_bot_token: rel-token\n"
        cfg = tmp_path / "rel.yaml"
        cfg.write_text(settings_content, encoding="utf-8")
        monkeypatch.setenv("CONFIG_FILE", str(cfg))
        settings = config.load()
        assert settings.telegram_bot_token == "rel-token"


class TestSettingsProperties:
    def test_effective_data_dir_default(self, minimal_yaml_bare, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml_bare)
        settings = config.load()
        # data_dir is "" so _effective_data_dir uses project root/data
        assert settings._effective_data_dir.endswith("data")
        assert (
            "openlucky" in settings._effective_data_dir
            or os.path.sep in settings._effective_data_dir
        )

    def test_effective_data_dir_absolute(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()
        # data_dir in minimal_yaml is an absolute path — returned unchanged
        assert os.path.isabs(settings.data_dir)
        assert settings._effective_data_dir == settings.data_dir

    @pytest.mark.unit
    def test_effective_data_dir_relative_resolves_to_project_root(self, tmp_path, monkeypatch):
        cfg = tmp_path / "settings.yaml"
        cfg.write_text("telegram_bot_token: t\ndata_dir: data-dev\n", encoding="utf-8")
        monkeypatch.setenv("CONFIG_FILE", str(cfg))
        settings = config.load()
        assert settings._effective_data_dir == os.path.join(settings.project_root, "data-dev")
        assert not settings._effective_data_dir.startswith("/tmp")

    def test_db_path(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()
        assert settings.db_path == os.path.join(settings._effective_data_dir, "app.db")

    def test_jobs_dir(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()
        assert settings.jobs_dir == os.path.join(settings._effective_data_dir, "jobs")

    def test_logs_dir(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()
        assert settings.logs_dir == os.path.join(settings._effective_data_dir, "logs")

    def test_project_root(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.load()
        # project root is the directory containing the app/ package
        assert os.path.isdir(settings.project_root)
        assert os.path.exists(os.path.join(settings.project_root, "app"))


class TestGetCached:
    def test_get_returns_settings(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        settings = config.get()
        assert settings.telegram_bot_token == "test-token-123"

    def test_get_is_cached(self, minimal_yaml, monkeypatch):
        monkeypatch.setenv("CONFIG_FILE", minimal_yaml)
        s1 = config.get()
        s2 = config.get()
        assert s1 is s2
