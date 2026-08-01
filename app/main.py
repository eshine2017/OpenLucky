"""
main.py — Entry point for the openlucky daemon.

Start with:
    python -m app.main
or:
    python app/main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from typing import Any

# Ensure the project root is on sys.path when running as a script.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from telegram.ext import ApplicationBuilder, MessageHandler, filters  # noqa: E402

from app import config, db, image_store  # noqa: E402
from app.agents.claude_code import ClaudeCodeAgent  # noqa: E402
from app.agents.gemini_code import GeminiAgent  # noqa: E402
from app.agents.registry import AgentRegistry  # noqa: E402
from app.bootstrap import BootstrapChecker  # noqa: E402
from app.command_router import CommandRouter  # noqa: E402
from app.context_builder import ContextBuilder  # noqa: E402
from app.daemon import Daemon  # noqa: E402
from app.scheduler import CronJob, Scheduler  # noqa: E402
from app.session_manager import SessionManager  # noqa: E402
from app.telegram_bot import TelegramBot  # noqa: E402

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # httpx logs every getUpdates long-poll at INFO, flooding the log with noise.
    # Incoming messages are still visible at INFO via telegram_bot.py's own log line.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _bootstrap_workspace(workspace_dir: str) -> None:
    """Copy template files into workspace_dir, skipping files that already exist."""
    template_dir = os.path.join(_PROJECT_ROOT, "config", "templates")
    memory_dir = os.path.join(workspace_dir, "memory")
    os.makedirs(memory_dir, exist_ok=True)

    for rel in ("SOUL.md", "USER.md", os.path.join("memory", "MEMORY.md")):
        src = os.path.join(template_dir, rel)
        dst = os.path.join(workspace_dir, rel)
        if not os.path.exists(src):
            logger.warning("Template missing: %s", src)
            continue
        if os.path.exists(dst):
            continue
        # Atomic create-if-missing: open with O_CREAT | O_EXCL
        try:
            fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh, open(src, encoding="utf-8") as src_fh:
                shutil.copyfileobj(src_fh, fh)
            logger.info("Created workspace file: %s", dst)
        except FileExistsError:
            pass  # another process beat us to it — fine


def main() -> None:
    # 1. Load configuration
    settings = config.get()
    _configure_logging(settings.log_level)

    logger.info("openlucky starting up…")

    # 2. Initialise database (also creates data/jobs and data/logs)
    db.init(settings.db_path, data_dir=settings._effective_data_dir)

    # Clean up images older than 24 hours from a previous run
    image_store.cleanup_old(settings.images_dir)

    # 3. Bootstrap workspace and create domain objects
    _bootstrap_workspace(settings.workspace_dir)

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
        default_model=settings.claude_model,
    )

    gemini_agent = GeminiAgent(
        gemini_bin=settings.gemini_bin,
        work_dir=settings.work_dir,
        gemini_model=settings.gemini_model,
        workspace_dir=settings.workspace_dir,
        second_brain_dir=settings.second_brain_dir,
        images_dir=settings.images_dir,
    )

    registry = AgentRegistry(
        agents={"claude": claude_agent, "gemini": gemini_agent},
        default=settings.provider,
    )

    session_manager = SessionManager(
        db=db,
        timeout_minutes=settings.session_timeout_minutes,
    )

    # 4. Thread-safe send_message callback for the Daemon.
    #
    #    PTB v20's run_polling() manages its own event loop internally.
    #    We capture that loop via a post_init hook so daemon threads can
    #    schedule coroutines onto it with run_coroutine_threadsafe.

    _loop_ref: list[asyncio.AbstractEventLoop] = []

    def send_message(chat_id: str, text: str) -> None:
        if not _loop_ref:
            logger.warning("send_message called before event loop is ready (chat=%s)", chat_id)
            return

        loop = _loop_ref[0]

        async def _send() -> None:
            await tg_app.bot.send_message(chat_id=int(chat_id), text=text)

        future = asyncio.run_coroutine_threadsafe(_send(), loop)
        try:
            future.result(timeout=15)
        except TimeoutError:
            logger.warning("send_message timed out for chat %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("send_message failed for chat %s: %s", chat_id, exc)

    # 5. Build scheduler and its callback.
    cron_spec_path = os.path.join(settings.workspace_dir, "cron.json")
    cron_state_path = os.path.join(settings._effective_data_dir, "cron-state.json")

    async def _scheduler_callback(job: CronJob) -> None:
        result = daemon.run_scheduled_job(prompt=job.prompt, label=job.id, model=job.model)
        logger.info("Scheduled job %r dispatch result: %s", job.id, result)

    scheduler = Scheduler(
        spec_path=cron_spec_path, state_path=cron_state_path, on_job=_scheduler_callback
    )

    # 6. Build the Telegram Application with a post_init hook that captures
    #    the running event loop and starts the scheduler.

    async def _post_init(_app: Any) -> None:
        loop = asyncio.get_running_loop()
        _loop_ref.append(loop)
        scheduler._loop = loop

        await scheduler.start()
        logger.info("Event loop captured; scheduler started; bot is ready.")

    async def _post_shutdown(_app: Any) -> None:
        await scheduler.stop()

    tg_app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # 7. Create Daemon and TelegramBot.
    # daemon must be constructed before command_router (command_router takes daemon)
    daemon = Daemon(
        db_module=db,
        agent=claude_agent,
        session_manager=session_manager,
        send_message_fn=send_message,
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
        scheduler=scheduler,
        daemon=daemon,
        registry=registry,
    )

    bot = TelegramBot(
        token=settings.telegram_bot_token,
        allowed_users=settings.allowed_users,
        daemon=daemon,
        command_router=command_router,
        images_dir=settings.images_dir,
    )
    bot._app = tg_app
    tg_app.add_handler(MessageHandler(filters.TEXT, bot._on_text_message))
    tg_app.add_handler(MessageHandler(filters.PHOTO, bot._on_photo_message))

    # 8. Hand control to PTB — it creates and manages its own event loop.
    logger.info("Bot polling started.")
    tg_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
