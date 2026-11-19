"""
tests/e2e/live/test_gemini_live.py — Opt-in live smoke test against the real Gemini CLI.

Skipped unless OPENLUCKY_E2E_LIVE=1 AND the configured binary is executable.
Run with: just test-e2e-live
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Opt-in gating
# ---------------------------------------------------------------------------

_LIVE_ENABLED = os.environ.get("OPENLUCKY_E2E_LIVE", "") == "1"
_GEMINI_BIN = os.environ.get("OPENLUCKY_E2E_LIVE_GEMINI_BIN") or shutil.which("gemini") or ""
_BINARY_EXECUTABLE = bool(_GEMINI_BIN) and os.access(_GEMINI_BIN, os.X_OK)

pytestmark = pytest.mark.skipif(
    not (_LIVE_ENABLED and _BINARY_EXECUTABLE),
    reason=(
        "Live smoke tests are opt-in: set OPENLUCKY_E2E_LIVE=1 and ensure "
        f"the gemini binary is executable (resolved to: {_GEMINI_BIN!r})"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_job(daemon: object, chat_id: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with daemon._lock:  # type: ignore[union-attr]
            if chat_id not in daemon.running_locks:  # type: ignore[union-attr]
                return
        time.sleep(0.1)
    raise TimeoutError(f"Job for chat {chat_id!r} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_gemini_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Isolated settings.yaml pointing at the real gemini binary."""
    import app.config as app_config

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Provide a minimal stub claude binary so the config loads cleanly.
    fake_claude = tmp_path / "fake_claude.sh"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'printf \'{"type":"result","session_id":"stub-sid","result":"stub"}\\n\'\n'
    )
    fake_claude.chmod(0o755)

    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(
        f'telegram_bot_token: "stub-token"\n'
        f'claude_bin: "{fake_claude}"\n'
        f'gemini_bin: "{_GEMINI_BIN}"\n'
        f'data_dir: "{data_dir}"\n'
        f'work_dir: "{work_dir}"\n'
        f'second_brain_dir: ""\n'
        f'provider: "gemini"\n'
    )

    monkeypatch.setenv("CONFIG_FILE", str(settings_path))
    app_config.get.cache_clear()

    yield app_config.get()

    app_config.get.cache_clear()


@pytest.fixture
def live_gemini_daemon(live_gemini_config):  # type: ignore[no-untyped-def]
    """Full-stack daemon wired to real Gemini CLI."""
    from app import db
    from app.agents.claude_code import ClaudeCodeAgent
    from app.agents.gemini_code import GeminiAgent
    from app.agents.registry import AgentRegistry
    from app.bootstrap import BootstrapChecker
    from app.context_builder import ContextBuilder
    from app.daemon import Daemon
    from app.main import _bootstrap_workspace
    from app.session_manager import SessionManager

    settings = live_gemini_config

    db.init(settings.db_path, data_dir=settings._effective_data_dir)

    _bootstrap_workspace(settings.workspace_dir)
    workspace = Path(settings.workspace_dir)
    (workspace / "SOUL.md").write_text(
        "# Bot Identity\n\n- **Name**: LiveTestBot\n- **Style**: concise\n"
    )
    (workspace / "USER.md").write_text(
        "# User Profile\n\n- **Name**: LiveTestUser\n- **Timezone**: UTC\n- **Language**: English\n"
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
        agent=gemini_agent,
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e_live
def test_gemini_live_happy_path(live_gemini_daemon) -> None:  # type: ignore[no-untyped-def]
    """
    One bounded prompt → real Gemini CLI → non-empty reply forwarded via send callback.
    Does not assert on exact LLM content — only on daemon plumbing.
    """
    from app import db as db_module
    from app.models import JobStatus

    daemon, send_mock = live_gemini_daemon
    chat_id = "live-gemini-100"

    daemon.on_message(chat_id, "Reply with exactly: OK")
    _wait_for_job(daemon, chat_id, timeout=120.0)

    # Assert the send callback was called at least once with a non-empty reply.
    assert send_mock.call_args_list, "Expected send callback to be called at least once"
    sent_texts = [str(call.args[1]) for call in send_mock.call_args_list]
    assert any(t.strip() for t in sent_texts), (
        f"Expected at least one non-empty message sent; got: {sent_texts}"
    )

    # Assert the job landed in SQLite with status 'done'.
    with db_module._lock:
        cur = db_module._get_conn().cursor()
        cur.execute(
            "SELECT status FROM jobs WHERE telegram_chat_id = ?"
            " ORDER BY started_at DESC LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()

    assert row is not None, "No job row found in the database for this chat"
    assert row[0] == JobStatus.done.value, f"Expected job status 'done', got {row[0]!r}"
