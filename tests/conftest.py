"""
tests/conftest.py — Shared fixtures for the openlucky test suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake CLI stub fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> str:
    """Write an executable shell script that emits a valid Claude stream-json result line."""
    session_id = f"test-session-{uuid.uuid4()}"
    script = tmp_path / "fake_claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"type":"result","session_id":"{session_id}","result":"stub reply"}}\\n\'\n'
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def fake_gemini_bin(tmp_path: Path) -> str:
    """Write an executable shell script that emits a valid Gemini JSON result line."""
    session_id = f"test-session-{uuid.uuid4()}"
    script = tmp_path / "fake_gemini.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"session_id":"{session_id}","response":"stub reply"}}\\n\'\n'
    )
    script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_config(
    tmp_path: Path,
    fake_claude_bin: str,
    fake_gemini_bin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator:
    """
    Render an isolated settings.yaml into tmp_path and point the app config at it.

    Clears the @lru_cache on both sides of the yield so config state cannot leak
    between tests.
    """
    import app.config as app_config

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(
        f'telegram_bot_token: "stub-token"\n'
        f'claude_bin: "{fake_claude_bin}"\n'
        f'gemini_bin: "{fake_gemini_bin}"\n'
        f'data_dir: "{data_dir}"\n'
        f'work_dir: "{work_dir}"\n'
        f'second_brain_dir: ""\n'
        f'provider: "claude"\n'
    )

    monkeypatch.setenv("CONFIG_FILE", str(settings_path))
    app_config.get.cache_clear()

    yield app_config.get()

    app_config.get.cache_clear()


# ---------------------------------------------------------------------------
# Workspace seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_workspace(e2e_config) -> str:  # type: ignore[no-untyped-def]
    """
    Bootstrap the workspace directory with template files, then overwrite SOUL.md
    and USER.md with "filled" content so BootstrapChecker returns COMPLETE.
    """
    from app.main import _bootstrap_workspace

    workspace_dir = e2e_config.workspace_dir
    _bootstrap_workspace(workspace_dir)

    workspace = Path(workspace_dir)
    (workspace / "SOUL.md").write_text(
        "# Bot Identity\n\n- **Name**: TestBot\n- **Style**: concise\n"
    )
    (workspace / "USER.md").write_text(
        "# User Profile\n\n- **Name**: TestUser\n- **Timezone**: UTC\n- **Language**: English\n"
    )

    return workspace_dir


# ---------------------------------------------------------------------------
# End-to-end daemon fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_daemon(e2e_config, seeded_workspace) -> tuple:  # type: ignore[no-untyped-def]
    """
    Construct a real Daemon wired to real db, SessionManager, AgentRegistry, and
    BootstrapChecker.  Returns (daemon, send_mock) so tests can assert on both.
    """
    from app import db
    from app.agents.claude_code import ClaudeCodeAgent
    from app.agents.gemini_code import GeminiAgent
    from app.agents.registry import AgentRegistry
    from app.bootstrap import BootstrapChecker
    from app.context_builder import ContextBuilder
    from app.daemon import Daemon
    from app.session_manager import SessionManager

    settings = e2e_config

    db.init(settings.db_path, data_dir=settings._effective_data_dir)

    context_builder = ContextBuilder(
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
    )
    bootstrap_checker = BootstrapChecker(
        workspace_dir=settings.workspace_dir,
        templates_dir=settings.templates_dir,
    )

    claude_agent = ClaudeCodeAgent(
        claude_bin=settings.claude_bin,
        work_dir=settings.work_dir,
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
        images_dir=settings.images_dir,
    )
    gemini_agent = GeminiAgent(
        gemini_bin=settings.gemini_bin,
        work_dir=settings.work_dir,
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
        images_dir=settings.images_dir,
    )

    registry = AgentRegistry(
        agents={"claude": claude_agent, "gemini": gemini_agent},
        default=settings.provider,
    )

    session_manager = SessionManager(db=db, timeout_minutes=settings.session_timeout_minutes)

    send_mock: MagicMock = MagicMock()

    cron_spec_path = str(Path(settings.workspace_dir) / "cron.json")

    daemon = Daemon(
        db_module=db,
        agent=claude_agent,
        session_manager=session_manager,
        send_message_fn=send_mock,
        jobs_dir=settings.jobs_dir,
        default_cwd=settings.work_dir,
        context_builder=context_builder,
        bootstrap_checker=bootstrap_checker,
        cron_spec_path=cron_spec_path,
        registry=registry,
    )

    return daemon, send_mock


# ---------------------------------------------------------------------------
# Factory fixtures for scenario tests
# ---------------------------------------------------------------------------


@pytest.fixture
def error_claude_bin(tmp_path: Path) -> str:
    """Fake claude binary that exits 1 and writes an error to stderr."""
    script = tmp_path / "error_claude.py"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "print('error: subprocess failed with an unexpected error', file=sys.stderr)",
                "sys.exit(1)",
                "",
            ]
        )
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def recording_claude_bin(tmp_path: Path) -> tuple[str, str]:
    """
    Fake claude binary that records each invocation's argv to a JSONL log file
    and emits a fixed-session-id result.

    Returns (bin_path, argv_log_path).  Read argv_log_path in tests to assert
    on what arguments were passed to the subprocess.
    """
    argv_log = str(tmp_path / "argv_log.jsonl")
    session_id = "recording-session-001"
    script = tmp_path / "recording_claude.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({argv_log!r}, 'a') as _f:\n"
        "    _f.write(json.dumps(sys.argv) + '\\n')\n"
        f"_sid = {session_id!r}\n"
        "print(json.dumps({'type':'result','session_id':_sid,'result':'stub reply'}))\n"
    )
    script.chmod(0o755)
    return str(script), argv_log


@pytest.fixture
def make_e2e_daemon(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    fake_gemini_bin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Callable[..., tuple[Any, MagicMock, Any]], None, None]:
    """
    Factory fixture: build an isolated full-stack daemon with a custom claude_bin.

    Call once per test::

        daemon, send_mock, settings = make_e2e_daemon(claude_bin="...", seed_workspace=True)

    seed_workspace=True  seeds SOUL.md and USER.md with filled content (bootstrap COMPLETE).
    seed_workspace=False leaves the workspace directory unpopulated (bootstrap NEEDED/MISSING).
    """
    import app.config as app_config

    _used = [False]

    def _make(
        claude_bin: str,
        seed_workspace: bool = True,
    ) -> tuple[Any, MagicMock, Any]:
        assert not _used[0], "make_e2e_daemon factory must be called at most once per test"
        _used[0] = True

        from app import db
        from app.agents.claude_code import ClaudeCodeAgent
        from app.agents.gemini_code import GeminiAgent
        from app.agents.registry import AgentRegistry
        from app.bootstrap import BootstrapChecker
        from app.context_builder import ContextBuilder
        from app.daemon import Daemon
        from app.main import _bootstrap_workspace
        from app.session_manager import SessionManager

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(exist_ok=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        work_dir = tmp_path / "work"
        work_dir.mkdir(exist_ok=True)

        settings_path = cfg_dir / "settings.yaml"
        settings_path.write_text(
            f'telegram_bot_token: "stub-token"\n'
            f'claude_bin: "{claude_bin}"\n'
            f'gemini_bin: "{fake_gemini_bin}"\n'
            f'data_dir: "{data_dir}"\n'
            f'work_dir: "{work_dir}"\n'
            f'second_brain_dir: ""\n'
            f'provider: "claude"\n'
        )

        monkeypatch.setenv("CONFIG_FILE", str(settings_path))
        app_config.get.cache_clear()
        settings = app_config.get()

        db.init(settings.db_path, data_dir=settings._effective_data_dir)

        if seed_workspace:
            _bootstrap_workspace(settings.workspace_dir)
            workspace = Path(settings.workspace_dir)
            (workspace / "SOUL.md").write_text(
                "# Bot Identity\n\n- **Name**: TestBot\n- **Style**: concise\n"
            )
            (workspace / "USER.md").write_text(
                "# User Profile\n\n- **Name**: TestUser\n"
                "- **Timezone**: UTC\n- **Language**: English\n"
            )

        context_builder = ContextBuilder(
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
        )
        bootstrap_checker = BootstrapChecker(
            workspace_dir=settings.workspace_dir,
            templates_dir=settings.templates_dir,
        )
        claude_agent = ClaudeCodeAgent(
            claude_bin=settings.claude_bin,
            work_dir=settings.work_dir,
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
            images_dir=settings.images_dir,
        )
        gemini_agent = GeminiAgent(
            gemini_bin=settings.gemini_bin,
            work_dir=settings.work_dir,
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
            images_dir=settings.images_dir,
        )
        registry = AgentRegistry(
            agents={"claude": claude_agent, "gemini": gemini_agent},
            default=settings.provider,
        )
        session_manager = SessionManager(db=db, timeout_minutes=settings.session_timeout_minutes)
        send_mock: MagicMock = MagicMock()
        cron_spec_path = str(Path(settings.workspace_dir) / "cron.json")

        daemon = Daemon(
            db_module=db,
            agent=claude_agent,
            session_manager=session_manager,
            send_message_fn=send_mock,
            jobs_dir=settings.jobs_dir,
            default_cwd=settings.work_dir,
            context_builder=context_builder,
            bootstrap_checker=bootstrap_checker,
            cron_spec_path=cron_spec_path,
            registry=registry,
        )

        return daemon, send_mock, settings

    yield _make

    app_config.get.cache_clear()
