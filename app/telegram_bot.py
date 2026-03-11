"""
telegram_bot.py — Telegram long-polling bot using python-telegram-bot v20 (async).
"""

from __future__ import annotations

import logging
from typing import List

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from app.command_router import CommandRouter
from app.daemon import Daemon

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Wraps python-telegram-bot v20.  Registers a single TEXT message handler
    that routes control commands to CommandRouter and everything else to Daemon.
    """

    def __init__(
        self,
        token: str,
        allowed_users: List[int],
        daemon: Daemon,
        command_router: CommandRouter,
        runner,
    ) -> None:
        self._token = token
        self._allowed_users = allowed_users
        self._daemon = daemon
        self._command_router = command_router
        self._runner = runner
        self._app: Application | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the Application and start long-polling (blocks until stopped)."""
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._on_text_message)
        )
        logger.info("Starting Telegram bot (long-polling)…")
        await self._app.run_polling(drop_pending_updates=True)

    def get_application(self) -> Application:
        """Return the underlying Application (needed to send messages from threads)."""
        if self._app is None:
            raise RuntimeError("Bot has not been started yet")
        return self._app

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    async def _on_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.message is None or update.effective_user is None:
            return

        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        text = update.message.text or ""

        logger.info(
            "Message from user %d (chat=%s): %r", user_id, chat_id, text[:80]
        )

        # Authorization check
        if self._allowed_users and user_id not in self._allowed_users:
            logger.warning("Unauthorized user %d tried to send a message", user_id)
            await update.message.reply_text("Unauthorized.")
            return

        # Command routing
        if self._command_router.is_command(text):
            response = self._command_router.handle(chat_id, text, self._runner)
            await update.message.reply_text(response)
            return

        # Regular message → hand off to daemon (non-blocking, runs in a thread)
        self._daemon.on_message(chat_id, text)
