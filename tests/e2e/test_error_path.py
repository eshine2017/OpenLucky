"""
E2E error-path scenario: the fake claude binary exits 1 with an error on
stderr.  The daemon must record the job as failed in the DB and send a
user-facing error message — it must not swallow the non-zero exit code.
"""

from __future__ import annotations

import time

import pytest

from app import db as db_module
from app.models import JobStatus

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


@pytest.mark.e2e
def test_error_path_job_fails_and_sends_error_message(
    error_claude_bin: str,
    make_e2e_daemon,  # type: ignore[no-untyped-def]
) -> None:
    """
    When the claude subprocess exits non-zero:
      - The job row in SQLite has status 'failed'.
      - send_mock is called with an error-indicating message (❌ or exit code).
      - The exit code is NOT swallowed.
    """
    daemon, send_mock, _settings = make_e2e_daemon(
        claude_bin=error_claude_bin, seed_workspace=True
    )

    chat_id = "500"
    daemon.on_message(chat_id, "do something that will fail")
    _wait_for_job(daemon, chat_id)

    # --- DB assertion: job must be recorded as failed ---
    with db_module._lock:
        cur = db_module._get_conn().cursor()
        cur.execute(
            "SELECT status, exit_code FROM jobs"
            " WHERE telegram_chat_id = ?"
            " ORDER BY started_at DESC LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()

    assert row is not None, "No job row found in the database for this chat"
    assert row[0] == JobStatus.failed.value, (
        f"Expected job status 'failed', got {row[0]!r}"
    )
    assert row[1] == 1, f"Expected exit_code 1, got {row[1]!r}"

    # --- send_mock assertion: an error message must have been sent ---
    sent_texts = [str(call.args[1]) for call in send_mock.call_args_list]
    assert any("❌" in t or "Failed" in t or "Exit code" in t for t in sent_texts), (
        f"Expected an error-indicating message in send calls; got: {sent_texts}"
    )
