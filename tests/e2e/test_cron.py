"""
E2E cron scenario: write a cron.json into the seeded workspace, call
run_scheduled_job directly (bypassing the timer), and assert the configured
prompt reaches the fake CLI stub and a reply is sent.
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

_CRON_PROMPT = "generate a daily digest of recent activity"
_CRON_JOB_ID = "daily-digest"


@pytest.mark.e2e
def test_cron_dispatches_prompt_and_sends_reply(
    recording_claude_bin: tuple[str, str],
    make_e2e_daemon,  # type: ignore[no-untyped-def]
) -> None:
    """
    Writing a cron.json and calling run_scheduled_job directly dispatches the
    configured prompt to the fake stub and delivers a reply via send_mock.
    """
    cron_bin, argv_log = recording_claude_bin
    daemon, send_mock, settings = make_e2e_daemon(claude_bin=cron_bin, seed_workspace=True)

    # Write a representative cron.json (the scheduler would read this file).
    cron_spec = {
        "jobs": [
            {
                "id": _CRON_JOB_ID,
                "name": "Daily Digest",
                "enabled": True,
                "cron_expr": "0 8 * * *",
                "tz": "UTC",
                "prompt": _CRON_PROMPT,
                "model": "",
            }
        ]
    }
    cron_path = Path(settings.workspace_dir) / "cron.json"
    cron_path.write_text(json.dumps(cron_spec, indent=2))

    # Seed a chat in the DB (run_scheduled_job requires get_most_recent_chat() to return one).
    chat_id = "400"
    daemon.on_message(chat_id, "hello")
    _wait_for_job(daemon, chat_id)

    send_count_before = send_mock.call_count

    # Dispatch the scheduled job directly, as the scheduler would after the timer fires.
    result = daemon.run_scheduled_job(prompt=_CRON_PROMPT, label=_CRON_JOB_ID)
    assert result == "dispatched", f"Expected 'dispatched', got {result!r}"

    _wait_for_job(daemon, chat_id)

    # A reply must have been sent after the scheduled job completed.
    assert send_mock.call_count > send_count_before, (
        "Expected at least one new send_mock call after the scheduled job"
    )

    # The configured prompt must have been passed to the subprocess.
    argv_lines = Path(argv_log).read_text().splitlines()
    argv_records = [json.loads(line) for line in argv_lines if line.strip()]

    # Find the -p argument across all invocations.
    all_prompts = [
        argv[argv.index("-p") + 1]
        for argv in argv_records
        if "-p" in argv and argv.index("-p") + 1 < len(argv)
    ]
    assert any(_CRON_PROMPT in p for p in all_prompts), (
        f"Cron prompt {_CRON_PROMPT!r} not found in any subprocess invocation. "
        f"Captured prompts: {[p[:120] for p in all_prompts]}"
    )
