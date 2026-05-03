"""Tests for context prefix injection in Daemon._build_prompt."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.context_builder import ContextBuilder
from app.daemon import Daemon
from app.models import RunResult, SessionDecision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(**kwargs) -> RunResult:
    defaults = dict(session_id="s1", stdout="", stderr="", exit_code=0, summary="done")
    defaults.update(kwargs)
    return RunResult(**defaults)


def _make_daemon(tmp_path, context_builder=None):
    jobs_dir = str(tmp_path / "jobs")
    mock_db = MagicMock()
    mock_agent = MagicMock()
    mock_agent.name = "claude"
    mock_session_manager = MagicMock()
    mock_send = MagicMock()

    daemon = Daemon(
        db_module=mock_db,
        agent=mock_agent,
        session_manager=mock_session_manager,
        send_message_fn=mock_send,
        jobs_dir=jobs_dir,
        default_cwd="/tmp/openlucky_work",
        context_builder=context_builder,
    )
    return daemon, mock_agent


def _decision(mode: str, session_id: str | None = None) -> SessionDecision:
    return SessionDecision(mode=mode, session_id=session_id)


# ---------------------------------------------------------------------------
# _build_prompt unit tests
# ---------------------------------------------------------------------------


def test_new_session_includes_prefix(tmp_path):
    mock_cb = MagicMock(spec=ContextBuilder)
    mock_cb.build_prefix.return_value = "IDENTITY BLOCK"

    daemon, _ = _make_daemon(tmp_path, context_builder=mock_cb)
    result = daemon._build_prompt("do the thing", "new")

    assert "IDENTITY BLOCK" in result
    assert "do the thing" in result
    mock_cb.build_prefix.assert_called_once()


def test_resume_session_excludes_prefix(tmp_path):
    mock_cb = MagicMock(spec=ContextBuilder)
    mock_cb.build_resume_hint.return_value = "Memory files: /some/path"

    daemon, _ = _make_daemon(tmp_path, context_builder=mock_cb)
    result = daemon._build_prompt("continue please", "resume")

    mock_cb.build_prefix.assert_not_called()
    mock_cb.build_resume_hint.assert_called_once()
    assert "continue please" in result
    assert "Memory files:" in result


def test_new_session_empty_prefix_returns_raw_message(tmp_path):
    mock_cb = MagicMock(spec=ContextBuilder)
    mock_cb.build_prefix.return_value = ""

    daemon, _ = _make_daemon(tmp_path, context_builder=mock_cb)
    result = daemon._build_prompt("hello", "new")

    assert result == "hello"


def test_no_context_builder_returns_raw_message(tmp_path):
    daemon, _ = _make_daemon(tmp_path, context_builder=None)
    result = daemon._build_prompt("raw message", "new")
    assert result == "raw message"


def test_prefix_build_failure_does_not_crash(tmp_path):
    mock_cb = MagicMock(spec=ContextBuilder)
    mock_cb.build_prefix.side_effect = RuntimeError("disk error")

    daemon, _ = _make_daemon(tmp_path, context_builder=mock_cb)
    # Should not raise; falls back to raw message
    result = daemon._build_prompt("safe fallback", "new")
    assert result == "safe fallback"


# ---------------------------------------------------------------------------
# Integration: prompt reaches agent with prefix
# ---------------------------------------------------------------------------


def test_full_job_new_session_prompt_has_prefix(tmp_path):
    mock_cb = MagicMock(spec=ContextBuilder)
    mock_cb.build_prefix.return_value = "<<SOUL>>"

    daemon, _ = _make_daemon(tmp_path, context_builder=mock_cb)

    built = daemon._build_prompt("my task", "new")
    assert "<<SOUL>>" in built
    assert "my task" in built
