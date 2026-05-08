"""Tests for app.command_router — command parsing and handler responses."""

from unittest.mock import MagicMock

import pytest

from app.command_router import CommandRouter
from app.context_builder import ContextBuilder
from app.models import ChatState, ChatStatus, Job, JobStatus


@pytest.fixture()
def mock_db():
    return MagicMock()


@pytest.fixture()
def mock_claude_agent():
    agent = MagicMock()
    agent.name = "claude"
    return agent


@pytest.fixture()
def router(mock_db, mock_claude_agent):
    return CommandRouter(db=mock_db, agent=mock_claude_agent)


class TestIsCommand:
    def test_known_commands(self, router) -> None:
        assert router.is_command("!status") is True
        assert router.is_command("!stop") is True
        assert router.is_command("!new") is True
        assert router.is_command("!reset") is True
        assert router.is_command("!cwd /tmp") is True
        assert router.is_command("!task build") is True

    def test_unknown_command(self, router) -> None:
        assert router.is_command("!unknown") is False

    def test_old_slash_prefix_not_recognised(self, router) -> None:
        assert router.is_command("/status") is False
        assert router.is_command("/stop") is False

    def test_not_a_command(self, router) -> None:
        assert router.is_command("hello") is False
        assert router.is_command("") is False

    def test_case_insensitive(self, router) -> None:
        assert router.is_command("!Status") is True
        assert router.is_command("!NEW") is True


class TestLooksLikeCommand:
    def test_known_command(self, router) -> None:
        assert router.looks_like_command("!status") is True

    def test_unknown_bang_prefix(self, router) -> None:
        assert router.looks_like_command("!nope") is True

    def test_old_slash_prefix(self, router) -> None:
        assert router.looks_like_command("/status") is False

    def test_plain_message(self, router) -> None:
        assert router.looks_like_command("hello") is False

    def test_empty_string(self, router) -> None:
        assert router.looks_like_command("") is False

    def test_leading_whitespace(self, router) -> None:
        assert router.looks_like_command("  !status") is True


class TestHandleStatus:
    def test_no_session(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "!status")
        assert "No active session" in result

    def test_with_session(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = ChatState(
            telegram_chat_id="1",
            active_session_id="s1",
            active_task_name="build",
            cwd="/tmp",
            status=ChatStatus.running,
            last_active_at="2025-01-01T00:00:00Z",
        )
        result = router.handle("1", "!status")
        assert "running" in result
        assert "build" in result
        assert "/tmp" in result
        assert "s1" in result


class TestHandleStop:
    def test_no_active_job(self, router, mock_db) -> None:
        mock_db.get_active_job.return_value = None
        result = router.handle("1", "!stop")
        assert "No task is currently running" in result

    def test_cancel_active_job(self, router, mock_db, mock_claude_agent) -> None:
        job = Job(job_id="j1-abcdef", telegram_chat_id="1", status=JobStatus.running)
        mock_db.get_active_job.return_value = job
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", status=ChatStatus.running)

        result = router.handle("1", "!stop")
        assert "Canceled" in result
        mock_claude_agent.cancel.assert_called_once_with("j1-abcdef")
        mock_db.update_job.assert_called_once()
        mock_db.upsert_chat.assert_called_once()


class TestHandleNew:
    def test_sets_force_new(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1")
        result = router.handle("1", "!new")
        assert "new session" in result
        mock_db.upsert_chat.assert_called_once()
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.force_new_next is True

    def test_no_existing_chat(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "!new")
        assert "new session" in result


class TestHandleReset:
    def test_no_session(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = None
        result = router.handle("1", "!reset")
        assert "No active session" in result

    def test_clear_session(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = ChatState(
            telegram_chat_id="1", active_session_id="s1-abcdef"
        )
        result = router.handle("1", "!reset")
        assert "cleared" in result.lower()
        mock_db.upsert_chat.assert_called_once()

    def test_no_bound_session(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", active_session_id=None)
        result = router.handle("1", "!reset")
        assert "No session was bound" in result


class TestHandleCwd:
    def test_no_path(self, router, mock_db) -> None:
        result = router.handle("1", "!cwd")
        assert "Usage" in result

    def test_relative_path(self, router, mock_db) -> None:
        result = router.handle("1", "!cwd relative/path")
        assert "absolute path" in result

    def test_valid_path(self, router, mock_db, tmp_path) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", cwd="/old")
        result = router.handle("1", f"!cwd {tmp_path}")
        assert "changed" in result.lower()
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.cwd == str(tmp_path)
        assert saved.force_new_next is True


class TestHandleTask:
    def test_no_name(self, router, mock_db) -> None:
        result = router.handle("1", "!task")
        assert "Usage" in result

    def test_set_name(self, router, mock_db) -> None:
        mock_db.get_chat.return_value = ChatState(telegram_chat_id="1", active_task_name="old")
        result = router.handle("1", "!task new-task")
        assert "new-task" in result
        saved = mock_db.upsert_chat.call_args[0][0]
        assert saved.active_task_name == "new-task"


class TestUnknownCommand:
    def test_unknown_bang_command_returns_help(self, router) -> None:
        result = router.handle("1", "!nope")
        assert "!status" in result
        assert "!stop" in result
        assert "!new" in result
        assert "!reset" in result
        assert "!cwd" in result
        assert "!task" in result
        assert "!soul" in result
        assert "!whoami" in result
        assert "!memory" in result


class TestMemoryCommands:
    @pytest.fixture()
    def mock_cb(self):
        return MagicMock(spec=ContextBuilder)

    @pytest.fixture()
    def router_with_cb(self, mock_db, mock_claude_agent, mock_cb):
        return CommandRouter(db=mock_db, agent=mock_claude_agent, context_builder=mock_cb)

    # --- !soul ---

    def test_soul_no_builder(self, router) -> None:
        result = router.handle("1", "!soul")
        assert "not configured" in result

    def test_soul_empty_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_soul.return_value = ""
        result = router_with_cb.handle("1", "!soul")
        assert "empty" in result or "template" in result

    def test_soul_returns_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_soul.return_value = "I am your assistant."
        result = router_with_cb.handle("1", "!soul")
        assert "I am your assistant." in result

    def test_soul_truncates_long_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_soul.return_value = "x" * 5000
        result = router_with_cb.handle("1", "!soul")
        assert len(result) <= 4096

    # --- !whoami ---

    def test_whoami_no_builder(self, router) -> None:
        result = router.handle("1", "!whoami")
        assert "not configured" in result

    def test_whoami_empty_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_user.return_value = ""
        result = router_with_cb.handle("1", "!whoami")
        assert "empty" in result or "template" in result

    def test_whoami_returns_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_user.return_value = "Name: Alice"
        result = router_with_cb.handle("1", "!whoami")
        assert "Name: Alice" in result

    def test_whoami_truncates_long_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_user.return_value = "y" * 5000
        result = router_with_cb.handle("1", "!whoami")
        assert len(result) <= 4096

    # --- !memory ---

    def test_memory_no_builder(self, router) -> None:
        result = router.handle("1", "!memory")
        assert "not configured" in result

    def test_memory_empty_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_memory.return_value = ""
        result = router_with_cb.handle("1", "!memory")
        assert "empty" in result or "template" in result

    def test_memory_returns_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_memory.return_value = "Remember: Python 3.12"
        result = router_with_cb.handle("1", "!memory")
        assert "Remember: Python 3.12" in result

    def test_memory_truncates_long_content(self, router_with_cb, mock_cb) -> None:
        mock_cb.read_memory.return_value = "z" * 5000
        result = router_with_cb.handle("1", "!memory")
        assert len(result) <= 4096

    # --- is_command recognises new commands ---

    def test_new_commands_recognised(self, router) -> None:
        assert router.is_command("!soul") is True
        assert router.is_command("!whoami") is True
        assert router.is_command("!memory") is True


class TestScheduleCommand:
    def test_no_scheduler_returns_not_configured(self, router) -> None:
        result = router.handle("1", "!schedule list")
        assert "not configured" in result

    def test_unknown_subcommand(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule pause")
        assert "Usage" in result

    def test_list_empty(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs.return_value = []
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule list")
        assert "No scheduled jobs" in result

    def test_list_with_jobs(self, mock_db, mock_claude_agent) -> None:
        from app.scheduler import CronJob, CronJobState

        job = CronJob(
            id="morning-digest",
            name="Morning digest",
            enabled=True,
            cron_expr="0 8 * * *",
            tz="UTC",
            prompt="Send morning digest",
            state=CronJobState(next_run_at_ms=1_700_000_000_000, last_status="ok"),
        )
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs.return_value = [job]
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule list")

        assert "morning-digest" in result
        assert "Morning digest" in result
        assert "ok" in result

    def test_list_disabled_job_shows_x(self, mock_db, mock_claude_agent) -> None:
        from app.scheduler import CronJob, CronJobState

        job = CronJob(
            id="j1",
            name="Disabled job",
            enabled=False,
            cron_expr="*/5 * * * *",
            tz="UTC",
            prompt="Check something",
            state=CronJobState(),
        )
        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs.return_value = [job]
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule list")
        assert "✗" in result

    def test_schedule_add_sets_pending_action(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        mock_daemon = MagicMock()
        mock_daemon.pending_actions = {}
        r = CommandRouter(
            db=mock_db,
            agent=mock_claude_agent,
            scheduler=mock_scheduler,
            daemon=mock_daemon,
        )
        result = r.handle("1", "!schedule add")
        assert mock_daemon.pending_actions.get("1") == "schedule_add"
        assert len(result) > 0

    def test_schedule_update_no_id_returns_usage(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule update")
        assert "Usage" in result
        assert "update" in result

    def test_schedule_update_unknown_id_returns_not_found(
        self, mock_db, mock_claude_agent
    ) -> None:
        from app.scheduler import CronJob, CronJobState

        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs.return_value = [
            CronJob(
                id="morning",
                name="Morning",
                enabled=True,
                cron_expr="0 8 * * *",
                tz="UTC",
                prompt="digest",
                state=CronJobState(),
            )
        ]
        mock_daemon = MagicMock()
        mock_daemon.pending_actions = {}
        r = CommandRouter(
            db=mock_db,
            agent=mock_claude_agent,
            scheduler=mock_scheduler,
            daemon=mock_daemon,
        )
        result = r.handle("1", "!schedule update unknown_id")
        assert "not found" in result

    def test_schedule_update_sets_pending_action(self, mock_db, mock_claude_agent) -> None:
        from app.scheduler import CronJob, CronJobState

        mock_scheduler = MagicMock()
        mock_scheduler.list_jobs.return_value = [
            CronJob(
                id="morning",
                name="Morning",
                enabled=True,
                cron_expr="0 8 * * *",
                tz="UTC",
                prompt="digest",
                state=CronJobState(),
            )
        ]
        mock_daemon = MagicMock()
        mock_daemon.pending_actions = {}
        r = CommandRouter(
            db=mock_db,
            agent=mock_claude_agent,
            scheduler=mock_scheduler,
            daemon=mock_daemon,
        )
        result = r.handle("1", "!schedule update morning")
        assert mock_daemon.pending_actions.get("1") == "schedule_update:morning"
        assert "morning" in result

    def test_schedule_run_found(self, mock_db, mock_claude_agent) -> None:
        import concurrent.futures
        import unittest.mock

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        future: concurrent.futures.Future[bool] = concurrent.futures.Future()
        future.set_result(True)
        mock_scheduler = MagicMock()
        mock_scheduler._loop = mock_loop
        with unittest.mock.patch("asyncio.run_coroutine_threadsafe", return_value=future):
            r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
            result = r.handle("1", "!schedule run my-job")
        assert "dispatched" in result

    def test_schedule_run_not_found(self, mock_db, mock_claude_agent) -> None:
        import concurrent.futures
        import unittest.mock

        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        future: concurrent.futures.Future[bool] = concurrent.futures.Future()
        future.set_result(False)
        mock_scheduler = MagicMock()
        mock_scheduler._loop = mock_loop
        with unittest.mock.patch("asyncio.run_coroutine_threadsafe", return_value=future):
            r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
            result = r.handle("1", "!schedule run bad-id")
        assert "not found" in result

    def test_schedule_remove_found(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        mock_scheduler.remove_job.return_value = True
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule remove my-job")
        assert "removed" in result

    def test_schedule_remove_not_found(self, mock_db, mock_claude_agent) -> None:
        mock_scheduler = MagicMock()
        mock_scheduler.remove_job.return_value = False
        r = CommandRouter(db=mock_db, agent=mock_claude_agent, scheduler=mock_scheduler)
        result = r.handle("1", "!schedule remove bad-id")
        assert "not found" in result

    def test_schedule_command_recognised(self, router) -> None:
        assert router.is_command("!schedule") is True
