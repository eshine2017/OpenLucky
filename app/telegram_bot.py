"""
telegram_bot.py — Telegram long-polling bot using python-telegram-bot v20 (async).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from app import image_store
from app.command_router import CommandRouter
from app.daemon import Daemon

type _App = Application[Any, Any, Any, Any, Any, Any]

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Wraps python-telegram-bot v20.  Registers a single TEXT message handler
    that routes control commands to CommandRouter and everything else to Daemon.
    """

    def __init__(
        self,
        token: str,
        allowed_users: list[int],
        daemon: Daemon,
        command_router: CommandRouter,
        images_dir: str = "",
    ) -> None:
        self._token = token
        self._allowed_users = allowed_users
        self._daemon = daemon
        self._command_router = command_router
        self._images_dir = images_dir
        self._app: _App | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the Application and start long-polling (blocks until stopped)."""
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT, self._on_text_message))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._on_photo_message))
        logger.info("Starting Telegram bot (long-polling)…")
        await self._app.run_polling(drop_pending_updates=True)  # type: ignore[func-returns-value]

    def get_application(self) -> _App:
        """Return the underlying Application (needed to send messages from threads)."""
        if self._app is None:
            raise RuntimeError("Bot has not been started yet")
        return self._app

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    async def _on_text_message(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return

        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)  # type: ignore[union-attr]
        text = update.message.text or ""

        logger.info("Message from user %d (chat=%s): %r", user_id, chat_id, text[:80])

        # Authorization check
        if self._allowed_users and user_id not in self._allowed_users:
            logger.warning("Unauthorized user %d tried to send a message", user_id)
            await update.message.reply_text("Unauthorized.")
            return

        # Command routing — any !-prefixed message goes to the router
        # (unknown !cmd returns the help list instead of reaching Claude)
        if self._command_router.looks_like_command(text):
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, self._command_router.handle, chat_id, text)
            await update.message.reply_text(response)
            return

        # Regular message → hand off to daemon (non-blocking, runs in a thread)
        self._daemon.on_message(chat_id, text)

    async def _on_photo_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.message is None or update.effective_user is None:
            return

        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)  # type: ignore[union-attr]
        caption = update.message.caption or ""

        logger.info("Photo from user %d (chat=%s)", user_id, chat_id)

        if self._allowed_users and user_id not in self._allowed_users:
            logger.warning("Unauthorized user %d tried to send a photo", user_id)
            await update.message.reply_text("Unauthorized.")
            return

        # Download the largest available photo size
        photo = update.message.photo[-1]
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            saved_path = image_store.save_telegram_photo(file_bytes, self._images_dir)
        except Exception as exc:
            logger.error("Failed to download/save photo from user %d: %s", user_id, exc)
            await update.message.reply_text(
                "Sorry, I couldn't download that image. Please try again."
            )
            return

        logger.info("Saved incoming photo to %s", saved_path)
        self._daemon.on_photo_message(chat_id, caption, [saved_path])
