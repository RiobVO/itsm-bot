"""Команды: /my сотруднику (F9), /stats, /ticket, /queue и /publish исполнителю.

Команды исполнителя объявлены с фильтром по типу чата прямо в декораторе, а не
проверяют чат внутри. В aiogram первый подошедший хендлер останавливает обработку
даже при обычном `return`: с проверкой внутри `/stats` в личке молча проглатывался
бы и не доходил до подсказки о неизвестной команде.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot import cards
from itsm_bot.bot.buffer import MessageBuffer
from itsm_bot.bot.handlers.profile import invite_to_profile
from itsm_bot.config import Settings
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language

logger = logging.getLogger(__name__)

router = Router(name="commands")

DEFAULT_STATS_DAYS = 30
IN_GROUP = F.chat.type.in_({"group", "supergroup"})


@router.message(CommandStart(), F.chat.type == "private")
async def start(
    message: Message, state: FSMContext, session: AsyncSession, buffer: MessageBuffer
) -> None:
    """Без этого хендлера `/start` дошёл бы до `messages` и стал бы заявкой «/start»."""
    employee = await repo.get_employee(session, str(message.from_user.id))
    if employee is None:
        await invite_to_profile(
            message,
            state,
            cards.language_from_code(message.from_user.language_code),
            buffer,
        )
        return

    await message.answer(cards.say("start_known", employee.language))


@router.message(Command("my"), F.chat.type == "private")
async def my_tickets(message: Message, session: AsyncSession) -> None:
    telegram_id = str(message.from_user.id)
    employee = await repo.get_employee(session, telegram_id)
    tickets = await repo.open_tickets(session, requester_id=telegram_id)

    await message.answer(
        cards.my_tickets_text(tickets, employee.language if employee else Language.RU)
    )


@router.message(Command("stats"), IN_GROUP)
async def stats(
    message: Message, command: CommandObject, session: AsyncSession, settings: Settings
) -> None:
    if message.chat.id != settings.queue_chat_id:
        return

    days = DEFAULT_STATS_DAYS
    if command.args and command.args.strip().isdigit():
        days = int(command.args.strip())

    summary = await repo.stats(session, window=timedelta(days=days))
    await message.answer(cards.stats_text(summary, days))


@router.message(Command("queue"), IN_GROUP)
async def show_queue(message: Message, session: AsyncSession, settings: Settings) -> None:
    """Открытые заявки списком — в том числе те, чья карточка не отправилась."""
    if message.chat.id != settings.queue_chat_id:
        return

    tickets = await repo.queue(session)
    if not tickets:
        await message.answer("Очередь пуста.")
        return

    lines = []
    for ticket in tickets:
        mark = "" if ticket.queue_message_id is not None else " (без карточки, /publish)"
        lines.append(
            f"{cards.STATUS_EMOJI[ticket.status]} #{ticket.id} · "
            f"каб. {ticket.room_snapshot}{mark}"
        )
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("publish"), IN_GROUP)
async def publish_card(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    settings: Settings,
    buffer: MessageBuffer,
    publish: Callable[[int], Awaitable[int | None]],
) -> None:
    """Досылает карточку заявки, оставшейся без неё из-за сбоя Telegram.

    Вручную, а не автоматически при старте: успешная отправка и запись
    `queue_message_id` разделены, и автоматическая досылка в окне между ними
    создала бы вторую карточку с живыми кнопками.
    """
    if message.chat.id != settings.queue_chat_id:
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер: /publish 47")
        return

    ticket = await repo.get_ticket(session, int(command.args.strip()))
    if ticket is None:
        await message.answer("Заявка не найдена.")
        return

    # Та же блокировка, под которой идёт обычная публикация: без неё команда,
    # поданная в тот момент, когда исходная отправка ещё выполняется, увидела бы
    # `queue_message_id = NULL` и отправила вторую карточку.
    async with buffer.lock(ticket.requester_id):
        await session.refresh(ticket)
        if ticket.queue_message_id is not None:
            await message.answer(f"У заявки #{ticket.id} карточка уже есть.")
            return

        message_id = await publish(ticket.id)
        if message_id is None:
            await message.answer(f"Не удалось отправить карточку заявки #{ticket.id}.")
            return

        await repo.set_queue_message_id(session, ticket.id, message_id)


@router.message(Command("ticket"), IN_GROUP)
async def full_ticket(
    message: Message, command: CommandObject, session: AsyncSession, settings: Settings
) -> None:
    """Полный текст заявки — то, что не поместилось в карточку (N12)."""
    if message.chat.id != settings.queue_chat_id:
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер: /ticket 47")
        return

    ticket = await repo.get_ticket(session, int(command.args.strip()))
    if ticket is None:
        await message.answer("Заявка не найдена.")
        return

    # Склеенная пачка легко перерастает лимит Telegram в 4096 символов, а отправка
    # без HTML нужна, чтобы текст с угловыми скобками вообще дошёл.
    for part in cards.chunks(f"Заявка #{ticket.id}\n\n{ticket.text}"):
        await message.answer(part, parse_mode=None)
