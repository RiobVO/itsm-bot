"""Приём сообщений сотрудника в личке.

Весь текст идёт в буфер склейки. Никаких решений здесь не принимается: что делать с
пачкой — ответ по заявке или новая заявка — решает `intake`, когда пачка собрана
целиком. Решать на каждом сообщении значило бы разорвать ответ, написанный
очередью, — ровно то, ради чего в проекте есть склейка (D7, D15).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot import cards
from itsm_bot.bot.buffer import MessageBuffer
from itsm_bot.bot.handlers.profile import invite_to_profile
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language

logger = logging.getLogger(__name__)

router = Router(name="messages")
router.message.filter(F.chat.type == "private")


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, session: AsyncSession) -> None:
    """Зарегистрирован раньше `incoming`: иначе опечатка `/halp` уехала бы в заявку.

    Известные команды сюда не доходят — их разбирают роутеры `profile` и `commands`,
    подключённые раньше.
    """
    employee = await repo.get_employee(session, str(message.from_user.id))
    await message.answer(
        cards.say("unknown_command", employee.language if employee else Language.RU)
    )


@router.message(F.text)
async def incoming(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    buffer: MessageBuffer,
) -> None:
    telegram_id = str(message.from_user.id)

    await buffer.add(
        telegram_id,
        text=message.text,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )

    if await repo.get_employee(session, telegram_id) is None:
        # Анкета удержит буфер, пока человек её заполняет. Вопрос про кабинет
        # задаётся только после нажатия кнопки — до него текст остаётся обращением.
        await invite_to_profile(
            message,
            state,
            cards.language_from_code(message.from_user.language_code),
            buffer,
        )
