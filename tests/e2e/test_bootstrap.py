"""
E2E bootstrap scenario:
  1. _bootstrap_workspace seeds template files into a fresh workspace.
  2. BootstrapChecker returns NEEDED when files are still at template content.
  3. Full state-machine run: send a message on a fresh workspace, the fake
     bootstrap binary fills SOUL.md / USER.md and emits COMPLETION_SENTINEL,
     and the daemon transitions to COMPLETE.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.bootstrap import COMPLETION_SENTINEL, BootstrapState
from app.models import ChatState

# ---------------------------------------------------------------------------
# Local helpers
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
# Local fixture: bootstrap-completing fake binary
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_complete_claude_bin(tmp_path: Path) -> str:
    """
    Fake claude binary that:
      1. Finds the workspace dir from --add-dir in its argv.
      2. Writes "filled" content to SOUL.md and USER.md.
      3. Emits COMPLETION_SENTINEL in the result field so the daemon detects completion.
    """
    script = tmp_path / "bootstrap_claude.py"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, sys",
                "",
                "workspace_dir = None",
                "args = sys.argv[1:]",
                "for i in range(len(args) - 1):",
                "    if args[i] == '--add-dir':",
                "        workspace_dir = args[i + 1]",
                "        break",
                "",
                "if workspace_dir:",
                "    soul = '# Bot Identity\\n\\n- **Name**: TestBot\\n- **Style**: concise\\n'",
                (
                    "    user = '# User Profile\\n\\n- **Name**: TestUser\\n"
                    "- **Timezone**: UTC\\n- **Language**: English\\n'"
                ),
                "    with open(os.path.join(workspace_dir, 'SOUL.md'), 'w') as f:",
                "        f.write(soul)",
                "    with open(os.path.join(workspace_dir, 'USER.md'), 'w') as f:",
                "        f.write(user)",
                "",
                f"sentinel = {COMPLETION_SENTINEL!r}",
                (
                    "print(json.dumps({'type': 'result', 'session_id': 'bootstrap-sess-001',"
                    " 'result': 'Setup done! ' + sentinel}))"
                ),
                "",
            ]
        )
    )
    script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_bootstrap_workspace_seeds_templates(e2e_config) -> None:  # type: ignore[no-untyped-def]
    """_bootstrap_workspace copies SOUL.md, USER.md, and memory/MEMORY.md from templates."""
    from app.main import _bootstrap_workspace

    workspace = Path(e2e_config.workspace_dir)

    # Workspace directory does not exist yet on a fresh config.
    assert not (workspace / "SOUL.md").exists(), "SOUL.md should not exist before seeding"
    assert not (workspace / "USER.md").exists(), "USER.md should not exist before seeding"

    _bootstrap_workspace(e2e_config.workspace_dir)

    assert (workspace / "SOUL.md").exists(), "SOUL.md must be created by _bootstrap_workspace"
    assert (workspace / "USER.md").exists(), "USER.md must be created by _bootstrap_workspace"
    assert (workspace / "memory" / "MEMORY.md").exists(), "memory/MEMORY.md must be created"


@pytest.mark.e2e
def test_bootstrap_checker_needed_on_template_content(e2e_config) -> None:  # type: ignore[no-untyped-def]
    """
    After _bootstrap_workspace, BootstrapChecker reports NEEDED because the
    files contain template markers ([[...]]) rather than real content.
    """
    from app.bootstrap import BootstrapChecker
    from app.main import _bootstrap_workspace

    _bootstrap_workspace(e2e_config.workspace_dir)

    checker = BootstrapChecker(
        workspace_dir=e2e_config.workspace_dir,
        templates_dir=e2e_config.templates_dir,
    )
    bs = checker.check(ChatState(telegram_chat_id="200"))
    assert bs.state == BootstrapState.NEEDED, (
        f"Expected NEEDED after seeding with templates; got {bs.state} "
        f"(soul={bs.soul}, user={bs.user})"
    )


@pytest.mark.e2e
def test_bootstrap_state_machine_completes(
    make_e2e_daemon,  # type: ignore[no-untyped-def]
    bootstrap_complete_claude_bin: str,
) -> None:
    """
    Fresh workspace → NEEDED; daemon dispatches bootstrap job; fake binary fills
    SOUL.md / USER.md and emits COMPLETION_SENTINEL → daemon transitions to COMPLETE
    and sends the "Setup complete!" confirmation message.
    """
    from app.bootstrap import BootstrapChecker
    from app.main import _bootstrap_workspace

    daemon, send_mock, settings = make_e2e_daemon(
        claude_bin=bootstrap_complete_claude_bin, seed_workspace=False
    )

    # Seed workspace with template files, mirroring what main.py does on startup.
    _bootstrap_workspace(settings.workspace_dir)

    checker = BootstrapChecker(
        workspace_dir=settings.workspace_dir,
        templates_dir=settings.templates_dir,
    )

    chat_id = "300"

    # State before first message: NEEDED (template files present, not filled).
    initial_bs = checker.check(ChatState(telegram_chat_id=chat_id))
    assert initial_bs.state == BootstrapState.NEEDED, (
        f"Expected NEEDED before any message; got {initial_bs.state}"
    )

    # The daemon sends "First-time setup..." immediately when it detects NEEDED.
    daemon.on_message(chat_id, "set me up please")
    _wait_for_job(daemon, chat_id)

    # Files are now filled and sentinel was emitted → COMPLETE.
    final_bs = checker.check(ChatState(telegram_chat_id=chat_id))
    assert final_bs.state == BootstrapState.COMPLETE, (
        f"Expected COMPLETE after bootstrap binary ran; "
        f"got {final_bs.state} (soul={final_bs.soul}, user={final_bs.user})"
    )

    # The daemon should have sent both the "First-time setup" notice and the
    # "Setup complete!" confirmation.
    sent_texts = [str(call.args[1]) for call in send_mock.call_args_list]
    assert any("First-time setup" in t or "setup" in t.lower() for t in sent_texts), (
        f"Expected a setup-initiation message; got: {sent_texts}"
    )
    assert any("Setup complete" in t for t in sent_texts), (
        f"Expected 'Setup complete' confirmation message; got: {sent_texts}"
    )
