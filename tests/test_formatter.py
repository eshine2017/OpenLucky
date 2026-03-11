"""Tests for app.formatter — pure formatting functions."""

from app.formatter import (
    format_done,
    format_error,
    format_running,
    format_start,
    truncate_for_telegram,
)


class TestFormatStart:
    def test_basic(self) -> None:
        result = format_start("my-task", "new", "/home/user")
        assert "my-task" in result
        assert "new" in result
        assert "/home/user" in result


class TestFormatRunning:
    def test_basic(self) -> None:
        assert "Running" in format_running()


class TestFormatDone:
    def test_basic(self) -> None:
        result = format_done("All good", 0, "/tmp/log.txt")
        assert "All good" in result
        assert "0" in result
        assert "/tmp/log.txt" in result


class TestFormatError:
    def test_basic(self) -> None:
        result = format_error("Something broke", 1)
        assert "Something broke" in result
        assert "1" in result


class TestTruncateForTelegram:
    def test_short_text_unchanged(self) -> None:
        text = "hello"
        assert truncate_for_telegram(text) == text

    def test_exact_boundary(self) -> None:
        text = "x" * 4000
        assert truncate_for_telegram(text) == text

    def test_over_limit_truncated(self) -> None:
        text = "x" * 5000
        result = truncate_for_telegram(text)
        assert len(result) <= 4000
        assert result.endswith("... (truncated)")

    def test_custom_max_length(self) -> None:
        text = "x" * 200
        result = truncate_for_telegram(text, max_length=100)
        assert len(result) <= 100
        assert result.endswith("... (truncated)")

    def test_just_over_limit(self) -> None:
        text = "x" * 4001
        result = truncate_for_telegram(text)
        assert len(result) <= 4000
        assert result.endswith("... (truncated)")
