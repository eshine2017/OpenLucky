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
import sys
from typing import Any

# Ensure the project root is on sys.path when running as a script.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from telegram.ext import ApplicationBuilder, MessageHandler, filters  # noqa: E402

from app import config, db  # noqa: E402
from app.claude_runner import ClaudeRunner  # noqa: E402
from app.command_router import CommandRouter  # noqa: E402
from app.daemon import Daemon  # noqa: E402
from app.session_manager import SessionManager  # noqa: E402
from app.telegram_bot import TelegramBot  # noqa: E402


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> None:
    # 1. Load configuration
    settings = config.get()
    _configure_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info("openlucky starting up…")

    # 2. Initialise database (also creates data/jobs and data/logs)
    data_dir = os.path.join(settings.project_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    db.init(settings.db_path, data_dir=data_dir)

    # 3. Create domain objects
    runner = ClaudeRunner(
        claude_bin=settings.claude_bin,
        work_dir=settings.work_dir,
    )

    session_manager = SessionManager(
        db=db,
        timeout_minutes=settings.session_timeout_minutes,
    )

    command_router = CommandRouter(
        db=db,
        session_manager=session_manager,
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

    # 5. Build the Telegram Application with a post_init hook that captures
    #    the running event loop once PTB has started it.

    async def _post_init(app: Any) -> None:
        _loop_ref.append(asyncio.get_running_loop())
        logger.info("Event loop captured; bot is ready.")

    tg_app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(_post_init).build()

    # 6. Create Daemon and TelegramBot.
    daemon = Daemon(
        db_module=db,
        runner=runner,
        session_manager=session_manager,
        send_message_fn=send_message,
        jobs_dir=settings.jobs_dir,
    )

    bot = TelegramBot(
        token=settings.telegram_bot_token,
        allowed_users=settings.allowed_users,
        daemon=daemon,
        command_router=command_router,
        runner=runner,
    )
    bot._app = tg_app  # type: ignore[attr-defined]
    tg_app.add_handler(MessageHandler(filters.TEXT, bot._on_text_message))

    # 7. Hand control to PTB — it creates and manages its own event loop.
    logger.info("Bot polling started.")
    tg_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
