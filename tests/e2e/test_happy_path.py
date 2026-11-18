"""E2E happy-path smoke test: message → fake CLI → stub reply via send callback."""

from __future__ import annotations

import time

import pytest

from app import db as db_module
from app.models import JobStatus


def _wait_for_job(daemon, chat_id: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with daemon._lock:
            if chat_id not in daemon.running_locks:
                return
        time.sleep(0.05)
    raise TimeoutError(f"Job for chat {chat_id!r} did not finish within {timeout}s")


@pytest.mark.e2e
def test_message_dispatches_claude_and_delivers_reply(e2e_daemon) -> None:  # type: ignore[no-untyped-def]
    """
    One message → fake claude binary invoked → "stub reply" forwarded to send callback
    → job row in SQLite has status 'done'.
    """
    daemon, send_mock = e2e_daemon
    chat_id = "100"

    daemon.on_message(chat_id, "hello from e2e test")
    _wait_for_job(daemon, chat_id)

    # Assert the stub reply reached the send callback at some point.
    sent_texts = [str(call.args[1]) for call in send_mock.call_args_list]
    assert any("stub reply" in t for t in sent_texts), (
        f"Expected 'stub reply' in one of the sent messages; got: {sent_texts}"
    )

    # Assert a job row landed in SQLite with status 'done'.
    with db_module._lock:
        cur = db_module._get_conn().cursor()
        cur.execute(
            "SELECT status FROM jobs WHERE telegram_chat_id = ?"
            " ORDER BY started_at DESC LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()

    assert row is not None, "No job row found in the database for this chat"
    assert row[0] == JobStatus.done.value, (
        f"Expected job status 'done', got {row[0]!r}"
    )
