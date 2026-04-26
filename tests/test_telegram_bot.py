"""Tests for app.telegram_bot — Telegram message handler: auth + routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.telegram_bot import TelegramBot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_daemon():
    daemon = MagicMock()
    daemon.on_message = MagicMock()
    return daemon


@pytest.fixture()
def mock_command_router():
    router = MagicMock()
    router.is_command = MagicMock(return_value=False)
    router.handle = MagicMock(return_value="command response")
    return router


@pytest.fixture()
def bot_no_restrictions(mock_daemon, mock_command_router):
    """TelegramBot with an empty allowed_users list (all users allowed)."""
    return TelegramBot(
        token="test-token",
        allowed_users=[],
        daemon=mock_daemon,
        command_router=mock_command_router,
    )


@pytest.fixture()
def bot_with_allowlist(mock_daemon, mock_command_router):
    """TelegramBot that only allows user 111."""
    return TelegramBot(
        token="test-token",
        allowed_users=[111],
        daemon=mock_daemon,
        command_router=mock_command_router,
    )


def _make_update(user_id: int, chat_id: str, text: str) -> MagicMock:
    """Build a minimal mock telegram Update object."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()

    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    update.effective_chat = MagicMock()
    update.effective_chat.id = int(chat_id)

    return update


def _make_context() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Guard clauses
# ---------------------------------------------------------------------------

class TestGuardClauses:
    @pytest.mark.asyncio
    async def test_returns_early_when_message_is_none(self, bot_no_restrictions):
        update = MagicMock()
        update.message = None
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        ctx = _make_context()

        # Should not raise
        await bot_no_restrictions._on_text_message(update, ctx)

        # daemon should not be called
        bot_no_restrictions._daemon.on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_user_is_none(self, bot_no_restrictions):
        update = MagicMock()
        update.message = MagicMock()
        update.effective_user = None
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        bot_no_restrictions._daemon.on_message.assert_not_called()


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

class TestAuthorization:
    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_rejected(self, bot_with_allowlist):
        update = _make_update(user_id=999, chat_id="42", text="hello")
        ctx = _make_context()

        await bot_with_allowlist._on_text_message(update, ctx)

        update.message.reply_text.assert_called_once_with("Unauthorized.")
        bot_with_allowlist._daemon.on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_user_passes_through(self, bot_with_allowlist):
        update = _make_update(user_id=111, chat_id="42", text="hello")
        ctx = _make_context()

        await bot_with_allowlist._on_text_message(update, ctx)

        # Should NOT reply with "Unauthorized."
        for call_args in update.message.reply_text.call_args_list:
            assert call_args[0][0] != "Unauthorized."

    @pytest.mark.asyncio
    async def test_empty_allowed_users_lets_everyone_through(self, bot_no_restrictions):
        update = _make_update(user_id=12345, chat_id="42", text="hello")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        # daemon should be called (not rejected)
        bot_no_restrictions._daemon.on_message.assert_called_once_with("42", "hello")


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------

class TestCommandRouting:
    @pytest.mark.asyncio
    async def test_command_routed_to_command_router(self, bot_no_restrictions, mock_command_router):
        mock_command_router.is_command.return_value = True
        mock_command_router.handle.return_value = "status: idle"
        update = _make_update(user_id=1, chat_id="42", text="/status")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        mock_command_router.is_command.assert_called_once_with("/status")
        mock_command_router.handle.assert_called_once_with("42", "/status")
        update.message.reply_text.assert_called_once_with("status: idle")

    @pytest.mark.asyncio
    async def test_command_does_not_reach_daemon(self, bot_no_restrictions, mock_command_router):
        mock_command_router.is_command.return_value = True
        update = _make_update(user_id=1, chat_id="42", text="/stop")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        bot_no_restrictions._daemon.on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_command_reaches_daemon(self, bot_no_restrictions, mock_command_router):
        mock_command_router.is_command.return_value = False
        update = _make_update(user_id=1, chat_id="42", text="write me a test")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        bot_no_restrictions._daemon.on_message.assert_called_once_with("42", "write me a test")
        update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# chat_id as string
# ---------------------------------------------------------------------------

class TestChatIdHandling:
    @pytest.mark.asyncio
    async def test_chat_id_converted_to_string(self, bot_no_restrictions):
        """effective_chat.id is an int in PTB; we must pass it as a str to daemon."""
        update = _make_update(user_id=1, chat_id="999", text="hello")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        bot_no_restrictions._daemon.on_message.assert_called_once_with("999", "hello")

    @pytest.mark.asyncio
    async def test_empty_text_still_passes_to_daemon(self, bot_no_restrictions):
        update = _make_update(user_id=1, chat_id="1", text="")
        ctx = _make_context()

        await bot_no_restrictions._on_text_message(update, ctx)

        bot_no_restrictions._daemon.on_message.assert_called_once_with("1", "")


# ---------------------------------------------------------------------------
# get_application()
# ---------------------------------------------------------------------------

class TestGetApplication:
    def test_raises_when_not_started(self, bot_no_restrictions):
        with pytest.raises(RuntimeError, match="not been started"):
            bot_no_restrictions.get_application()
