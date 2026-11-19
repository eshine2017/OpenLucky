"""
E2E image-handling scenario: send a photo message carrying an image payload.
Assert that:
  1. The image file is persisted under <data_dir>/images/.
  2. The image path is passed to the agent subprocess (--add-dir images_dir).
  3. The fake stub is invoked and delivers a reply via send_mock.
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


@pytest.mark.e2e
def test_image_persisted_and_path_forwarded_to_subprocess(
    recording_claude_bin: tuple[str, str],
    make_e2e_daemon,  # type: ignore[no-untyped-def]
) -> None:
    """
    Sending a photo message persists the image to images_dir and passes
    --add-dir <images_dir> to the claude subprocess.
    """
    cron_bin, argv_log = recording_claude_bin
    daemon, send_mock, settings = make_e2e_daemon(claude_bin=cron_bin, seed_workspace=True)

    # Create the images directory and write a fake JPEG.
    images_dir = Path(settings.images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    image_path = images_dir / "test-photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake jpeg bytes")

    assert image_path.exists(), "Pre-condition: test image must exist on disk"

    chat_id = "600"
    daemon.on_photo_message(chat_id, "describe this image", [str(image_path)])
    _wait_for_job(daemon, chat_id)

    # --- Image file exists under images_dir ---
    assert image_path.exists(), "Image file must still be present under images_dir"

    # --- send_mock must have been called (reply delivered) ---
    assert send_mock.called, "Expected send_mock to be called after the photo job completed"

    # --- The subprocess must have received --add-dir <images_dir> ---
    argv_lines = Path(argv_log).read_text().splitlines()
    argv_records = [json.loads(line) for line in argv_lines if line.strip()]
    assert argv_records, "No argv records found — subprocess was not invoked"

    images_dir_str = str(images_dir)
    assert any(
        "--add-dir" in argv and images_dir_str in argv
        for argv in argv_records
    ), (
        f"Expected '--add-dir {images_dir_str}' in subprocess argv; "
        f"got: {[' '.join(a) for a in argv_records]}"
    )
