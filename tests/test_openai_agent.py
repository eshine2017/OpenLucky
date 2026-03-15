"""Tests for app.agents.openai_agent — OpenAI streaming agent with disk session history."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.models import RunResult


# ---------------------------------------------------------------------------
# Helpers to create a mock openai module before OpenAIAgent is imported
# ---------------------------------------------------------------------------

def _make_mock_openai_module() -> ModuleType:
    """Return a fake openai module with a stub OpenAI client class."""
    mock_openai = ModuleType("openai")
    mock_client = MagicMock()
    mock_openai.OpenAI = MagicMock(return_value=mock_client)
    return mock_openai


def _make_agent(sessions_dir: str, mock_openai: ModuleType | None = None):
    """Instantiate OpenAIAgent with a patched openai module."""
    if mock_openai is None:
        mock_openai = _make_mock_openai_module()

    with patch.dict(sys.modules, {"openai": mock_openai}):
        from app.agents.openai_agent import OpenAIAgent  # noqa: PLC0415
        agent = OpenAIAgent(api_key="sk-test", model="gpt-4o-mini", sessions_dir=sessions_dir)

    return agent, mock_openai


def _make_chunk(content: str) -> MagicMock:
    chunk = MagicMock()
    chunk.choices[0].delta.content = content
    return chunk


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sessions_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return str(d)


@pytest.fixture()
def mock_openai():
    return _make_mock_openai_module()


@pytest.fixture()
def agent(sessions_dir, mock_openai):
    a, _ = _make_agent(sessions_dir, mock_openai)
    return a


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_client(self, sessions_dir):
        mock_openai = _make_mock_openai_module()
        agent, _ = _make_agent(sessions_dir, mock_openai)
        mock_openai.OpenAI.assert_called_once_with(api_key="sk-test")
        assert agent._model == "gpt-4o-mini"
        assert agent._sessions_dir == sessions_dir

    def test_cancelled_set_empty_on_init(self, agent):
        assert agent._cancelled == set()


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

class TestRun:
    def test_successful_run_returns_result(self, agent, mock_openai):
        chunks = [_make_chunk("Hello"), _make_chunk(" world")]
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(chunks)

        result = agent.run(prompt="Hi", cwd="/tmp", session_id="s1", job_id="j1")

        assert isinstance(result, RunResult)
        assert result.exit_code == 0
        assert result.stdout == "Hello world"
        assert result.session_id == "s1"
        assert result.stderr == ""

    def test_assigns_new_session_id_when_none_given(self, agent, mock_openai):
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(
            [_make_chunk("ok")]
        )
        result = agent.run(prompt="Hi", cwd="/tmp", session_id=None, job_id="j2")
        # session_id must be a non-empty UUID-like string
        assert result.session_id
        assert len(result.session_id) > 8

    def test_summary_truncated_at_3000(self, agent, mock_openai):
        long_text = "x" * 4000
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(
            [_make_chunk(long_text)]
        )
        result = agent.run(prompt="q", cwd="/tmp", session_id=None, job_id="j3")
        assert "truncated" in result.summary
        assert len(result.summary) <= 3014  # 3000 + len("\n… (truncated)")

    def test_history_appended_and_saved(self, agent, mock_openai, sessions_dir):
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(
            [_make_chunk("answer")]
        )
        agent.run(prompt="question", cwd="/tmp", session_id="s-hist", job_id="jh")

        # Session file should now exist
        import os
        session_file = os.path.join(sessions_dir, "s-hist.json")
        assert os.path.exists(session_file)
        with open(session_file, encoding="utf-8") as fh:
            history = json.load(fh)
        assert history[-2]["role"] == "user"
        assert history[-2]["content"] == "question"
        assert history[-1]["role"] == "assistant"
        assert history[-1]["content"] == "answer"

    def test_existing_history_loaded_and_sent(self, agent, mock_openai, sessions_dir):
        """Previous messages should be included in the API call."""
        import os
        existing = [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "done"}]
        session_file = os.path.join(sessions_dir, "s-existing.json")
        with open(session_file, "w", encoding="utf-8") as fh:
            json.dump(existing, fh)

        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(
            [_make_chunk("new reply")]
        )
        agent.run(prompt="follow-up", cwd="/tmp", session_id="s-existing", job_id="j-ex")

        call_kwargs = mock_openai.OpenAI.return_value.chat.completions.create.call_args
        messages = call_kwargs[1]["messages"] if call_kwargs[1] else call_kwargs[0][1]
        # Should contain the old history plus new user message
        roles = [m["role"] for m in messages]
        assert roles[0] == "user"
        assert roles[1] == "assistant"
        assert roles[2] == "user"

    def test_chunk_with_none_content_skipped(self, agent, mock_openai):
        """A chunk whose delta.content is None should contribute nothing."""
        chunks = [_make_chunk("part1"), _make_chunk(None), _make_chunk("part2")]
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter(chunks)
        result = agent.run(prompt="q", cwd="/tmp", session_id=None, job_id="j-none")
        assert result.stdout == "part1part2"


# ---------------------------------------------------------------------------
# run() — OpenAI exception path
# ---------------------------------------------------------------------------

class TestRunException:
    def test_openai_exception_returns_error_result(self, agent, mock_openai):
        mock_openai.OpenAI.return_value.chat.completions.create.side_effect = RuntimeError(
            "network failure"
        )
        result = agent.run(prompt="q", cwd="/tmp", session_id=None, job_id="j-err")
        assert result.exit_code == 1
        assert "network failure" in result.stderr
        assert "OpenAI error" in result.summary


# ---------------------------------------------------------------------------
# cancel() and cancellation during run()
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_adds_to_cancelled_set(self, agent):
        agent.cancel("job-abc")
        assert "job-abc" in agent._cancelled

    def test_cancel_before_start_returns_cancelled_result(self, agent, mock_openai):
        """If job_id is cancelled before run() executes, return immediately."""
        agent.cancel("pre-cancelled-job")
        # stream should NOT be called
        mock_openai.OpenAI.return_value.chat.completions.create.return_value = iter([])

        result = agent.run(prompt="q", cwd="/tmp", session_id=None, job_id="pre-cancelled-job")
        assert result.exit_code == 1
        assert result.summary == "(cancelled)"
        assert "Cancelled" in result.stderr
        # Cancelled key should be consumed
        assert "pre-cancelled-job" not in agent._cancelled

    def test_cancel_mid_stream_stops_processing(self, agent, mock_openai):
        """If cancel() is called after the first chunk, subsequent chunks are skipped."""
        call_count = 0

        def _chunks():
            nonlocal call_count
            yield _make_chunk("first")
            call_count += 1
            # Simulate cancel happening after first chunk
            agent.cancel("mid-job")
            yield _make_chunk("second")  # should be skipped
            call_count += 1

        mock_openai.OpenAI.return_value.chat.completions.create.return_value = _chunks()

        result = agent.run(prompt="q", cwd="/tmp", session_id=None, job_id="mid-job")
        # Exit code 0 because we broke out of the loop (no exception), just incomplete
        assert result.exit_code == 0
        # Only "first" was accumulated before cancel
        assert result.stdout == "first"


# ---------------------------------------------------------------------------
# _load_history()
# ---------------------------------------------------------------------------

class TestLoadHistory:
    def test_returns_empty_when_no_file(self, agent, sessions_dir):
        result = agent._load_history("nonexistent-session")
        assert result == []

    def test_returns_messages_from_valid_file(self, agent, sessions_dir):
        import os
        data = [{"role": "user", "content": "hello"}]
        path = os.path.join(sessions_dir, "valid-session.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        result = agent._load_history("valid-session")
        assert result == data

    def test_returns_empty_on_corrupt_json(self, agent, sessions_dir):
        import os
        path = os.path.join(sessions_dir, "corrupt.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        result = agent._load_history("corrupt")
        assert result == []

    def test_returns_empty_on_oserror(self, agent, sessions_dir):
        """If opening the file raises OSError, return empty list."""
        import os
        path = os.path.join(sessions_dir, "unreadable.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        # Make it unreadable
        os.chmod(path, 0o000)
        try:
            result = agent._load_history("unreadable")
            assert result == []
        finally:
            os.chmod(path, 0o644)


# ---------------------------------------------------------------------------
# _save_history()
# ---------------------------------------------------------------------------

class TestSaveHistory:
    def test_saves_to_file(self, agent, sessions_dir):
        import os
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        agent._save_history("save-test", history)

        path = os.path.join(sessions_dir, "save-test.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert loaded == history

    def test_creates_sessions_dir_if_missing(self, tmp_path):
        new_dir = str(tmp_path / "new_sessions")
        mock_openai = _make_mock_openai_module()
        agent_obj, _ = _make_agent(new_dir, mock_openai)
        # sessions_dir doesn't exist yet
        import os
        assert not os.path.exists(new_dir)
        agent_obj._save_history("s1", [{"role": "user", "content": "hi"}])
        assert os.path.exists(new_dir)

    def test_oserror_logged_but_not_raised(self, agent, sessions_dir):
        """An OSError during save should be caught and not propagate."""
        with patch("builtins.open", side_effect=OSError("disk full")):
            # Should NOT raise
            agent._save_history("oserror-test", [])
