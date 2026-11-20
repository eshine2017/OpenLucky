"""E2E transcript test for !version command."""

from __future__ import annotations

import pytest


@pytest.mark.e2e
@pytest.mark.record_transcript
def test_version_command(transcript_recorder) -> None:  # type: ignore[no-untyped-def]
    daemon, send_mock, recorder = transcript_recorder
    chat_id = "999"
    recorder.send_user(chat_id, "!version")
    sent_texts = [str(call.args[1]) for call in send_mock.call_args_list]
    assert any("openlucky v0.1.0" in t for t in sent_texts)
