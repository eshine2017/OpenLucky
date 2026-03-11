"""Tests for app.claude_runner — command building and output parsing."""

import json

from app.claude_runner import ClaudeRunner


def _make_runner() -> ClaudeRunner:
    return ClaudeRunner(claude_bin="claude", work_dir="/tmp/test_work")


class TestBuildCommand:
    def test_without_session(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hello world", session_id=None)
        assert cmd == [
            "claude",
            "-p",
            "hello world",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def test_with_session(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hello", session_id="sess-123")
        assert cmd == [
            "claude",
            "-p",
            "hello",
            "--output-format",
            "stream-json",
            "--verbose",
            "--resume",
            "sess-123",
        ]

    def test_empty_session_id_not_added(self) -> None:
        runner = _make_runner()
        cmd = runner._build_command("hi", session_id="")
        assert "--resume" not in cmd


class TestParseStreamJson:
    def test_valid_result_event(self) -> None:
        runner = _make_runner()
        output = json.dumps(
            {
                "type": "result",
                "session_id": "abc-123",
                "result": "All done!",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "abc-123"
        assert summary == "All done!"

    def test_no_result_event_fallback_to_assistant(self) -> None:
        runner = _make_runner()
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Working on it..."}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "Here is the answer."}],
                    },
                }
            ),
        ]
        output = "\n".join(lines)
        session_id, summary = runner._parse_stream_json(output)
        assert session_id is None
        # Falls back to last assistant text
        assert summary == "Here is the answer."

    def test_empty_output(self) -> None:
        runner = _make_runner()
        session_id, summary = runner._parse_stream_json("")
        assert session_id is None
        assert summary == "(No summary available)"

    def test_invalid_json_lines_skipped(self) -> None:
        runner = _make_runner()
        output = "not json\n{bad json too\n" + json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": "ok",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "s1"
        assert summary == "ok"

    def test_long_summary_truncated(self) -> None:
        runner = _make_runner()
        long_text = "x" * 4000
        output = json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": long_text,
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert len(summary) <= 3020  # 3000 + len("… (truncated)") + newline
        assert "truncated" in summary

    def test_result_without_session_id(self) -> None:
        runner = _make_runner()
        output = json.dumps(
            {
                "type": "result",
                "result": "Some output",
            }
        )
        session_id, summary = runner._parse_stream_json(output)
        assert session_id is None
        assert summary == "Some output"

    def test_mixed_events(self) -> None:
        runner = _make_runner()
        lines = [
            json.dumps({"type": "system", "data": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "thinking..."}]},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "s99",
                    "result": "Final answer",
                }
            ),
        ]
        output = "\n".join(lines)
        session_id, summary = runner._parse_stream_json(output)
        assert session_id == "s99"
        assert summary == "Final answer"
