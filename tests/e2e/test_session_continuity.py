"""
E2E session-continuity scenario: send two messages in the same chat.
Assert that the second subprocess invocation reuses the session_id from the
first via --resume, rather than starting a fresh session.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _wait_for_job(daemon: object, chat_id: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with daemon._lock:  # type: ignore[union-attr]
            if chat_id not in daemon.running_locks:  # type: ignore[union-attr]
                return
        time.sleep(0.05)
    raise TimeoutError(f"Job for chat {chat_id!r} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

_RESUMED_SESSION_ID = "recording-session-001"  # fixed ID emitted by recording_claude_bin


@pytest.mark.e2e
@pytest.mark.record_transcript
def test_second_message_resumes_existing_session(
    recording_claude_bin: tuple[str, str],
    make_e2e_daemon,  # type: ignore[no-untyped-def]
    make_transcript_recorder,  # type: ignore[no-untyped-def]
) -> None:
    """
    Message A → new session, session_id captured from stub output.
    Message B in the same chat → --resume <session_id> passed to subprocess.
    """
    rec_bin, argv_log = recording_claude_bin
    daemon, send_mock, _settings = make_e2e_daemon(claude_bin=rec_bin, seed_workspace=True)
    recorder = make_transcript_recorder(daemon, send_mock)

    chat_id = "700"

    # First message: starts a new session.
    recorder.send_user(chat_id, "first task: write a hello world")
    _wait_for_job(daemon, chat_id)

    # Second message: should resume the session established by the first job.
    # Using a neutral message that does not trigger new-task heuristics.
    recorder.send_user(chat_id, "also add a test for that")
    _wait_for_job(daemon, chat_id)

    # Read the argv log — one entry per subprocess invocation.
    argv_lines = Path(argv_log).read_text().splitlines()
    argv_records = [json.loads(line) for line in argv_lines if line.strip()]

    assert len(argv_records) == 2, (
        f"Expected exactly 2 subprocess invocations; got {len(argv_records)}"
    )

    # First invocation: no --resume flag.
    assert "--resume" not in argv_records[0], (
        "First message should start a new session (no --resume)"
    )

    # Second invocation: must include --resume <session_id>.
    assert "--resume" in argv_records[1], (
        "Second message should resume the existing session (expected --resume)"
    )
    resume_idx = argv_records[1].index("--resume")
    actual_session_id = argv_records[1][resume_idx + 1]
    assert actual_session_id == _RESUMED_SESSION_ID, (
        f"Expected --resume {_RESUMED_SESSION_ID!r}; got {actual_session_id!r}"
    )
