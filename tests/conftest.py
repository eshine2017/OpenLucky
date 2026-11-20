"""
tests/conftest.py — Shared fixtures for the openlucky test suite.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Transcript recorder
# ---------------------------------------------------------------------------

_ARTIFACTS_DIR = Path(__file__).parent / "e2e" / "artifacts"
_transcript_index: list[dict[str, str]] = []
_transcript_test_results: dict[str, str] = {}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _nodeid_to_filename(nodeid: str) -> str:
    return re.sub(r"[^\w.-]", "_", nodeid) + ".html"


class TranscriptRecorder:
    """Records inbound (user) and outbound (bot) messages for one test."""

    def __init__(self, daemon: Any) -> None:
        self._daemon = daemon
        self.log: list[dict[str, str]] = []

    def send_user(self, chat_id: str, text: str) -> None:
        """Record a user message then dispatch it to the daemon."""
        self.log.append({"dir": "user", "chat_id": chat_id, "text": text, "ts": _iso_now()})
        self._daemon.on_message(chat_id, text)


def _attach_recorder(recorder: TranscriptRecorder, send_mock: MagicMock) -> None:
    """Wrap send_mock's side_effect so bot sends are appended to recorder.log."""
    orig = send_mock.side_effect

    def _wrap(*args: Any, **kwargs: Any) -> Any:
        chat_id = str(args[0]) if args else ""
        text = str(args[1]) if len(args) > 1 else str(kwargs.get("text", ""))
        # Detect image placeholder: path under <data_dir>/images/
        display = f"[image: {Path(text).name}]" if re.search(r"/images/[^/]+$", text) else text
        recorder.log.append({"dir": "bot", "chat_id": chat_id, "text": display, "ts": _iso_now()})
        if orig is not None:
            return orig(*args, **kwargs)
        return None

    send_mock.side_effect = _wrap


def _write_transcript_html(nodeid: str, log: list[dict[str, str]]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / _nodeid_to_filename(nodeid)

    bubbles: list[str] = []
    for entry in log:
        cls = "user" if entry["dir"] == "user" else "bot"
        text_esc = html.escape(entry["text"]).replace("\n", "<br>")
        meta_esc = html.escape(f"{entry.get('chat_id', '')} · {entry.get('ts', '')}")
        bubbles.append(
            f'<div class="bubble {cls}">'
            f'<div class="msg"><div class="text">{text_esc}</div>'
            f'<div class="meta">{meta_esc}</div></div></div>'
        )

    title = html.escape(nodeid)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#e5ddd5;min-height:100vh;padding:1rem}}
h1{{font-size:.8rem;color:#555;word-break:break-all;margin-bottom:1rem;padding:.5rem;background:#fff;border-radius:4px}}
.chat{{max-width:600px;margin:0 auto;display:flex;flex-direction:column;gap:.5rem}}
.bubble{{display:flex}}
.bubble.user{{justify-content:flex-end}}
.bubble.bot{{justify-content:flex-start}}
.msg{{max-width:75%;padding:.5rem .75rem;border-radius:12px;word-break:break-word}}
.bubble.user .msg{{background:#dcf8c6;border-radius:12px 12px 2px 12px}}
.bubble.bot .msg{{background:#fff;border-radius:12px 12px 12px 2px;
box-shadow:0 1px 2px rgba(0,0,0,.12)}}
.text{{font-size:.9rem;line-height:1.4}}
.meta{{font-size:.65rem;color:#888;margin-top:.25rem;text-align:right}}
.bubble.bot .meta{{text-align:left}}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="chat">
{chr(10).join(bubbles)}
</div>
</body>
</html>"""

    path.write_text(page, encoding="utf-8")
    return path


def _write_index_html(entries: list[dict[str, str]]) -> None:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for entry in entries:
        outcome = entry.get("outcome", "unknown")
        color = "green" if outcome == "passed" else ("red" if outcome == "failed" else "#888")
        nid = html.escape(entry["nodeid"])
        link = html.escape(Path(entry["path"]).name)
        rows.append(
            f'<tr><td><a href="{link}">{nid}</a></td>'
            f'<td style="color:{color};font-weight:bold">{outcome}</td></tr>'
        )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>E2E Transcript Index</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;padding:1rem;max-width:900px;margin:0 auto}}
h1{{margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:.5rem .75rem;text-align:left}}
th{{background:#f5f5f5}}
a{{color:#1a73e8;text-decoration:none}}
a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<h1>E2E Transcript Index</h1>
<table>
<thead><tr><th>Test</th><th>Outcome</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
</body>
</html>"""

    (_ARTIFACTS_DIR / "index.html").write_text(page, encoding="utf-8")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Any:  # type: ignore[type-arg]
    result = yield
    if result.when == "call":
        _transcript_test_results[item.nodeid] = result.outcome
    return result


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _transcript_index:
        _write_index_html(_transcript_index)


@pytest.fixture
def transcript_recorder(request: pytest.FixtureRequest, e2e_daemon: tuple) -> Generator:  # type: ignore[type-arg]
    """
    Wraps e2e_daemon with a TranscriptRecorder.  Yields (daemon, send_mock, recorder).

    Use recorder.send_user(chat_id, text) instead of daemon.on_message so inbound
    messages appear in the HTML.  Existing send_mock.call_args_list assertions work
    unchanged — the mock is wrapped, not replaced.

    When the test is marked @pytest.mark.record_transcript and the log is non-empty,
    a Telegram-style HTML file is written to tests/e2e/artifacts/ at teardown.
    """
    daemon, send_mock = e2e_daemon
    recorder = TranscriptRecorder(daemon)
    _attach_recorder(recorder, send_mock)
    daemon._transcript_recorder = recorder  # type: ignore[attr-defined]

    yield daemon, send_mock, recorder

    if request.node.get_closest_marker("record_transcript") and recorder.log:
        path = _write_transcript_html(request.node.nodeid, recorder.log)
        outcome = _transcript_test_results.get(request.node.nodeid, "unknown")
        _transcript_index.append(
            {"nodeid": request.node.nodeid, "path": str(path), "outcome": outcome}
        )


@pytest.fixture
def make_transcript_recorder(request: pytest.FixtureRequest) -> Generator:  # type: ignore[type-arg]
    """
    Factory fixture for tests that build their daemon via make_e2e_daemon.

    Usage::

        recorder = make_transcript_recorder(daemon, send_mock)
        recorder.send_user(chat_id, "hello")

    At teardown, writes HTML if the test is marked @pytest.mark.record_transcript.
    """
    _recorders: list[tuple[TranscriptRecorder, str]] = []

    def _factory(daemon: Any, send_mock: MagicMock) -> TranscriptRecorder:
        recorder = TranscriptRecorder(daemon)
        _attach_recorder(recorder, send_mock)
        daemon._transcript_recorder = recorder  # type: ignore[attr-defined]
        _recorders.append((recorder, request.node.nodeid))
        return recorder

    yield _factory

    if request.node.get_closest_marker("record_transcript"):
        for recorder, nodeid in _recorders:
            if recorder.log:
                path = _write_transcript_html(nodeid, recorder.log)
                outcome = _transcript_test_results.get(nodeid, "unknown")
                _transcript_index.append(
                    {"nodeid": nodeid, "path": str(path), "outcome": outcome}
                )


# ---------------------------------------------------------------------------
# Fake CLI stub fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> str:
    """Write an executable shell script that emits a valid Claude stream-json result line."""
    session_id = f"test-session-{uuid.uuid4()}"
    script = tmp_path / "fake_claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"type":"result","session_id":"{session_id}","result":"stub reply"}}\\n\'\n'
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def fake_gemini_bin(tmp_path: Path) -> str:
    """Write an executable shell script that emits a valid Gemini JSON result line."""
    session_id = f"test-session-{uuid.uuid4()}"
    script = tmp_path / "fake_gemini.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"session_id":"{session_id}","response":"stub reply"}}\\n\'\n'
    )
    script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_config(
    tmp_path: Path,
    fake_claude_bin: str,
    fake_gemini_bin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator:
    """
    Render an isolated settings.yaml into tmp_path and point the app config at it.

    Clears the @lru_cache on both sides of the yield so config state cannot leak
    between tests.
    """
    import app.config as app_config

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(
        f'telegram_bot_token: "stub-token"\n'
        f'claude_bin: "{fake_claude_bin}"\n'
        f'gemini_bin: "{fake_gemini_bin}"\n'
        f'data_dir: "{data_dir}"\n'
        f'work_dir: "{work_dir}"\n'
        f'second_brain_dir: ""\n'
        f'provider: "claude"\n'
    )

    monkeypatch.setenv("CONFIG_FILE", str(settings_path))
    app_config.get.cache_clear()

    yield app_config.get()

    app_config.get.cache_clear()


# ---------------------------------------------------------------------------
# Workspace seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_workspace(e2e_config) -> str:  # type: ignore[no-untyped-def]
    """
    Bootstrap the workspace directory with template files, then overwrite SOUL.md
    and USER.md with "filled" content so BootstrapChecker returns COMPLETE.
    """
    from app.main import _bootstrap_workspace

    workspace_dir = e2e_config.workspace_dir
    _bootstrap_workspace(workspace_dir)

    workspace = Path(workspace_dir)
    (workspace / "SOUL.md").write_text(
        "# Bot Identity\n\n- **Name**: TestBot\n- **Style**: concise\n"
    )
    (workspace / "USER.md").write_text(
        "# User Profile\n\n- **Name**: TestUser\n- **Timezone**: UTC\n- **Language**: English\n"
    )

    return workspace_dir


# ---------------------------------------------------------------------------
# End-to-end daemon fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_daemon(e2e_config, seeded_workspace) -> tuple:  # type: ignore[no-untyped-def]
    """
    Construct a real Daemon wired to real db, SessionManager, AgentRegistry, and
    BootstrapChecker.  Returns (daemon, send_mock) so tests can assert on both.
    """
    from app import db
    from app.agents.claude_code import ClaudeCodeAgent
    from app.agents.gemini_code import GeminiAgent
    from app.agents.registry import AgentRegistry
    from app.bootstrap import BootstrapChecker
    from app.command_router import CommandRouter
    from app.context_builder import ContextBuilder
    from app.daemon import Daemon
    from app.session_manager import SessionManager

    settings = e2e_config

    db.init(settings.db_path, data_dir=settings._effective_data_dir)

    context_builder = ContextBuilder(
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
    )
    bootstrap_checker = BootstrapChecker(
        workspace_dir=settings.workspace_dir,
        templates_dir=settings.templates_dir,
    )

    claude_agent = ClaudeCodeAgent(
        claude_bin=settings.claude_bin,
        work_dir=settings.work_dir,
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
        images_dir=settings.images_dir,
    )
    gemini_agent = GeminiAgent(
        gemini_bin=settings.gemini_bin,
        work_dir=settings.work_dir,
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
        images_dir=settings.images_dir,
    )

    registry = AgentRegistry(
        agents={"claude": claude_agent, "gemini": gemini_agent},
        default=settings.provider,
    )

    session_manager = SessionManager(db=db, timeout_minutes=settings.session_timeout_minutes)

    send_mock: MagicMock = MagicMock()

    cron_spec_path = str(Path(settings.workspace_dir) / "cron.json")

    daemon = Daemon(
        db_module=db,
        agent=claude_agent,
        session_manager=session_manager,
        send_message_fn=send_mock,
        jobs_dir=settings.jobs_dir,
        default_cwd=settings.work_dir,
        context_builder=context_builder,
        bootstrap_checker=bootstrap_checker,
        cron_spec_path=cron_spec_path,
        registry=registry,
    )

    command_router = CommandRouter(
        db=db,
        agent=claude_agent,
        context_builder=context_builder,
        bootstrap_checker=bootstrap_checker,
        registry=registry,
        daemon=daemon,
    )
    daemon._command_router = command_router

    return daemon, send_mock


# ---------------------------------------------------------------------------
# Factory fixtures for scenario tests
# ---------------------------------------------------------------------------


@pytest.fixture
def error_claude_bin(tmp_path: Path) -> str:
    """Fake claude binary that exits 1 and writes an error to stderr."""
    script = tmp_path / "error_claude.py"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "print('error: subprocess failed with an unexpected error', file=sys.stderr)",
                "sys.exit(1)",
                "",
            ]
        )
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def recording_claude_bin(tmp_path: Path) -> tuple[str, str]:
    """
    Fake claude binary that records each invocation's argv to a JSONL log file
    and emits a fixed-session-id result.

    Returns (bin_path, argv_log_path).  Read argv_log_path in tests to assert
    on what arguments were passed to the subprocess.
    """
    argv_log = str(tmp_path / "argv_log.jsonl")
    session_id = "recording-session-001"
    script = tmp_path / "recording_claude.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({argv_log!r}, 'a') as _f:\n"
        "    _f.write(json.dumps(sys.argv) + '\\n')\n"
        f"_sid = {session_id!r}\n"
        "print(json.dumps({'type':'result','session_id':_sid,'result':'stub reply'}))\n"
    )
    script.chmod(0o755)
    return str(script), argv_log


@pytest.fixture
def make_e2e_daemon(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    fake_gemini_bin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Callable[..., tuple[Any, MagicMock, Any]], None, None]:
    """
    Factory fixture: build an isolated full-stack daemon with a custom claude_bin.

    Call once per test::

        daemon, send_mock, settings = make_e2e_daemon(claude_bin="...", seed_workspace=True)

    seed_workspace=True  seeds SOUL.md and USER.md with filled content (bootstrap COMPLETE).
    seed_workspace=False leaves the workspace directory unpopulated (bootstrap NEEDED/MISSING).
    """
    import app.config as app_config

    _used = [False]

    def _make(
        claude_bin: str,
        seed_workspace: bool = True,
    ) -> tuple[Any, MagicMock, Any]:
        assert not _used[0], "make_e2e_daemon factory must be called at most once per test"
        _used[0] = True

        from app import db
        from app.agents.claude_code import ClaudeCodeAgent
        from app.agents.gemini_code import GeminiAgent
        from app.agents.registry import AgentRegistry
        from app.bootstrap import BootstrapChecker
        from app.context_builder import ContextBuilder
        from app.daemon import Daemon
        from app.main import _bootstrap_workspace
        from app.session_manager import SessionManager

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(exist_ok=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        work_dir = tmp_path / "work"
        work_dir.mkdir(exist_ok=True)

        settings_path = cfg_dir / "settings.yaml"
        settings_path.write_text(
            f'telegram_bot_token: "stub-token"\n'
            f'claude_bin: "{claude_bin}"\n'
            f'gemini_bin: "{fake_gemini_bin}"\n'
            f'data_dir: "{data_dir}"\n'
            f'work_dir: "{work_dir}"\n'
            f'second_brain_dir: ""\n'
            f'provider: "claude"\n'
        )

        monkeypatch.setenv("CONFIG_FILE", str(settings_path))
        app_config.get.cache_clear()
        settings = app_config.get()

        db.init(settings.db_path, data_dir=settings._effective_data_dir)

        if seed_workspace:
            _bootstrap_workspace(settings.workspace_dir)
            workspace = Path(settings.workspace_dir)
            (workspace / "SOUL.md").write_text(
                "# Bot Identity\n\n- **Name**: TestBot\n- **Style**: concise\n"
            )
            (workspace / "USER.md").write_text(
                "# User Profile\n\n- **Name**: TestUser\n"
                "- **Timezone**: UTC\n- **Language**: English\n"
            )

        context_builder = ContextBuilder(
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
        )
        bootstrap_checker = BootstrapChecker(
            workspace_dir=settings.workspace_dir,
            templates_dir=settings.templates_dir,
        )
        claude_agent = ClaudeCodeAgent(
            claude_bin=settings.claude_bin,
            work_dir=settings.work_dir,
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
            images_dir=settings.images_dir,
        )
        gemini_agent = GeminiAgent(
            gemini_bin=settings.gemini_bin,
            work_dir=settings.work_dir,
            workspace_dir=settings.workspace_dir,
            second_brain_dir=settings.second_brain_dir,
            images_dir=settings.images_dir,
        )
        registry = AgentRegistry(
            agents={"claude": claude_agent, "gemini": gemini_agent},
            default=settings.provider,
        )
        session_manager = SessionManager(db=db, timeout_minutes=settings.session_timeout_minutes)
        send_mock: MagicMock = MagicMock()
        cron_spec_path = str(Path(settings.workspace_dir) / "cron.json")

        daemon = Daemon(
            db_module=db,
            agent=claude_agent,
            session_manager=session_manager,
            send_message_fn=send_mock,
            jobs_dir=settings.jobs_dir,
            default_cwd=settings.work_dir,
            context_builder=context_builder,
            bootstrap_checker=bootstrap_checker,
            cron_spec_path=cron_spec_path,
            registry=registry,
        )

        return daemon, send_mock, settings

    yield _make

    app_config.get.cache_clear()
