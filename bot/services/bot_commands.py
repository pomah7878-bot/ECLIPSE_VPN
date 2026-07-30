"""Telegram bot command menu synchronization."""
from __future__ import annotations

import logging
from typing import Any

from aiogram.types import BotCommand, MenuButtonCommands

logger = logging.getLogger(__name__)

MAX_BOT_COMMANDS = 100

CORE_BOT_COMMANDS = (
    BotCommand(command='start', description='🏠 Главное меню'),
    BotCommand(command='buy', description='💎 Купить VPN'),
    BotCommand(command='mykeys', description='🔑 Мои ключи'),
    BotCommand(command='ai', description='🤖 Спросить AI'),
    BotCommand(command='support', description='💬 Поддержка'),
    BotCommand(command='help', description='📖 Инструкция'),
    BotCommand(command='id', description='🆔 Мой ID'),
)


def build_bot_commands() -> list[BotCommand]:
    """Builds the Bot API command list from core and extension commands."""
    from bot.utils.extension_commands import get_extension_command_definitions

    commands = list(CORE_BOT_COMMANDS)
    used = {command.command for command in commands}
    for definition in get_extension_command_definitions():
        if definition.command in used:
            continue
        commands.append(BotCommand(command=definition.command, description=definition.description))
        used.add(definition.command)
        if len(commands) >= MAX_BOT_COMMANDS:
            logger.warning(
                "Telegram command menu reached %s commands; extra extension commands are skipped",
                MAX_BOT_COMMANDS,
            )
            break
    return commands


async def sync_bot_commands(bot: Any) -> bool:
    """Sends the current command menu to Telegram."""
    if bot is None or not callable(getattr(bot, 'set_my_commands', None)):
        raise ValueError('bot must support set_my_commands')
    commands = build_bot_commands()
    await bot.set_my_commands(commands)
    logger.info("Telegram command menu synchronized: %s commands", len(commands))
    return True


async def sync_menu_button(bot):
    """Sets the chat menu button to show the command list on tap
    (вместо WebApp — так пользователь видит доступные команды визуально,
    а не только вводом /). WebApp остаётся доступен через кнопки в
    главном меню чата и вложение WebApp у бота."""
    if bot is None or not callable(getattr(bot, 'set_chat_menu_button', None)):
        raise ValueError('bot must support set_chat_menu_button')

    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Кнопка меню установлена: список команд")
    return True


__all__ = [
    'CORE_BOT_COMMANDS',
    'MAX_BOT_COMMANDS',
    'build_bot_commands',
    'sync_bot_commands',
    'sync_menu_button',
]
