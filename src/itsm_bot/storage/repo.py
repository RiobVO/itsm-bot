"""Операции над заявками.

Слой между хендлерами бота и БД: хендлеры не пишут SQL, а вызывают эти функции.
Каждая фиксирует транзакцию сама — при 5 заявках в день (N2) нет сценария, где
несколько операций должны попасть в одну транзакцию.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.storage.models import (
    Employee,
    MessageDirection,
    Ticket,
    TicketMessage,
    TicketStatus,
    utcnow,
)

logger = logging.getLogger(__name__)

CLOSED_STATUSES = (TicketStatus.DONE, TicketStatus.CANCELLED)

_WHITESPACE = re.compile(r"\s+")


def text_hash(text: str) -> str:
    """Хеш текста для дедупликации (N11).

    Регистр и переносы строк нормализуются: человек, повторяющий обращение, редко
    воспроизводит его посимвольно, а пачка сообщений склеивается переводами строки.
    """
    normalized = _WHITESPACE.sub(" ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def get_employee(session: AsyncSession, telegram_id: str) -> Employee | None:
    return await session.get(Employee, telegram_id)


async def save_employee(
    session: AsyncSession,
    *,
    telegram_id: str,
    display_name: str | None,
    room: str,
    department: str,
) -> Employee:
    """Создаёт профиль или обновляет существующий (F1, F3)."""
    employee = await session.get(Employee, telegram_id)
    if employee is None:
        employee = Employee(telegram_id=telegram_id)
        session.add(employee)

    employee.display_name = display_name
    employee.room = room
    employee.department = department

    await session.commit()
    return employee


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def create_ticket(
    session: AsyncSession,
    *,
    employee: Employee,
    text: str,
    security_flag: bool,
    source_chat_id: int,
    source_message_id: int,
) -> Ticket:
    """Заводит заявку, копируя кабинет и отдел из профиля на текущий момент.

    Копия, а не ссылка: сотрудник переедет, а заявка должна помнить, куда тогда
    шёл исполнитель.
    """
    ticket = Ticket(
        requester_id=employee.telegram_id,
        text=text,
        status=TicketStatus.NEW,
        room_snapshot=employee.room,
        department_snapshot=employee.department,
        security_flag=security_flag,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        text_hash=text_hash(text),
    )
    session.add(ticket)
    await session.commit()

    logger.info(
        "Заявка #%s создана сотрудником %s, security_flag=%s",
        ticket.id,
        employee.telegram_id,
        security_flag,
    )
    return ticket


async def set_status(
    session: AsyncSession, ticket_id: int, status: TicketStatus
) -> Ticket | None:
    """Меняет статус и синхронно правит отметки времени.

    Возврат из закрытого статуса очищает `closed_at`: иначе заявка окажется
    закрытой по времени и открытой по статусу, и отчёт по длительности соврёт.
    """
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        logger.warning("Попытка сменить статус несуществующей заявки #%s", ticket_id)
        return None

    ticket.status = status

    if status is TicketStatus.IN_PROGRESS and ticket.taken_at is None:
        ticket.taken_at = utcnow()

    ticket.closed_at = utcnow() if status in CLOSED_STATUSES else None

    await session.commit()
    logger.info("Заявка #%s переведена в статус %s", ticket_id, status.value)
    return ticket


async def set_queue_message_id(
    session: AsyncSession, ticket_id: int, queue_message_id: int
) -> None:
    """Запоминает карточку в канале, чтобы потом перерисовывать её кнопки."""
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        logger.warning("Карточка привязана к несуществующей заявке #%s", ticket_id)
        return

    ticket.queue_message_id = queue_message_id
    await session.commit()


async def find_recent_duplicate(
    session: AsyncSession, *, requester_id: str, text: str, window: timedelta
) -> Ticket | None:
    """Ищет ту же заявку от того же человека за окно (N11).

    Одинаковый текст от разных людей дублем не считается: про один сломанный
    принтер пишут несколько человек, и это разные обращения.
    """
    statement = (
        select(Ticket)
        .where(
            Ticket.requester_id == requester_id,
            Ticket.text_hash == text_hash(text),
            Ticket.created_at >= utcnow() - window,
        )
        .order_by(Ticket.id.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def count_recent_tickets(
    session: AsyncSession, *, requester_id: str, window: timedelta
) -> int:
    """Сколько заявок сотрудник создал за окно — для рейт-лимита (N10)."""
    statement = select(func.count(Ticket.id)).where(
        Ticket.requester_id == requester_id,
        Ticket.created_at >= utcnow() - window,
    )
    return (await session.execute(statement)).scalar_one()


async def open_tickets(session: AsyncSession, *, requester_id: str) -> list[Ticket]:
    """Незакрытые заявки сотрудника для /my (F9)."""
    statement = (
        select(Ticket)
        .where(
            Ticket.requester_id == requester_id,
            Ticket.status.not_in(CLOSED_STATUSES),
        )
        .order_by(Ticket.id)
    )
    return list((await session.execute(statement)).scalars())


async def queue(session: AsyncSession) -> list[Ticket]:
    """Незакрытые заявки всех сотрудников — очередь исполнителя."""
    statement = (
        select(Ticket).where(Ticket.status.not_in(CLOSED_STATUSES)).order_by(Ticket.id)
    )
    return list((await session.execute(statement)).scalars())


async def add_message(
    session: AsyncSession, ticket_id: int, direction: MessageDirection, text: str
) -> TicketMessage:
    """Добавляет реплику в переписку по заявке (F8)."""
    message = TicketMessage(ticket_id=ticket_id, direction=direction, text=text)
    session.add(message)
    await session.commit()
    return message


async def ticket_messages(session: AsyncSession, ticket_id: int) -> list[TicketMessage]:
    statement = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.id)
    )
    return list((await session.execute(statement)).scalars())
