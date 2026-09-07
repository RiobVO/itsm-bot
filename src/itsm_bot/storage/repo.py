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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.security.redactor import PII_PLACEHOLDER, SECRET_PLACEHOLDER, redact
from itsm_bot.storage.models import (
    Employee,
    Language,
    MessageDirection,
    Ticket,
    TicketMessage,
    TicketStatus,
    utcnow,
)

logger = logging.getLogger(__name__)

CLOSED_STATUSES = (TicketStatus.DONE, TicketStatus.CANCELLED)

_WHITESPACE = re.compile(r"\s+")


def mask(text: str) -> tuple[str, bool]:
    """Маскирует текст и говорит, были ли в нём секреты.

    Замаскированный текст при повторном прогоне не меняется, а вот признак находки
    — меняется: во второй раз `redact()` видит уже только плейсхолдеры и возвращает
    `found=False`. Если бы флаг брался прямо оттуда, текст, замаскированный где-то
    выше по конвейеру, сохранился бы с `security_flag=False`, и сотрудник не получил
    бы предупреждения о присланном пароле.

    Поэтому уже готовый плейсхолдер во входе считается таким же признаком, как и
    собственная находка. Сотрудник, приславший «[REDACTED:secret]» вручную, получит
    лишнее предупреждение — это дешевле пропущенного настоящего секрета.
    """
    already_masked = SECRET_PLACEHOLDER in text or PII_PLACEHOLDER in text
    result = redact(text)
    return result.text, result.found or already_masked


def text_hash(text: str) -> str:
    """Хеш текста для дедупликации (N11).

    Нормализуются только пробелы и переносы строк — пачка сообщений склеивается
    через них, и одно и то же обращение не должно разойтись по такой мелочи.

    Регистр намеренно сохраняется. Ошибка дедупа асимметрична: лишняя заявка стоит
    одного клика, потерянная — пропавшего обращения. Понижение регистра слило бы
    `/opt/App` и `/opt/app` в один хеш, а типичный повтор человек присылает
    копипастой, где регистр и так совпадает.
    """
    normalized = _WHITESPACE.sub(" ", text).strip()
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
    language: Language,
) -> Employee:
    """Создаёт профиль или обновляет существующий (F1, F3).

    Кабинет маскируется здесь по той же причине, что и текст заявки: это
    единственное поле профиля со свободным вводом, и любой сбой анкеты превращает
    его в приёмник произвольного текста. Требование N8 не должно зависеть от того,
    все ли ветки хендлеров написаны правильно.
    """
    employee = await session.get(Employee, telegram_id)
    if employee is None:
        employee = Employee(telegram_id=telegram_id)
        session.add(employee)

    employee.display_name = display_name
    employee.room, _ = mask(room)
    employee.department = department
    employee.language = language

    await session.commit()
    return employee


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def create_ticket(
    session: AsyncSession,
    *,
    employee: Employee,
    text: str,
    source_chat_id: int,
    source_message_id: int,
) -> Ticket:
    """Заводит заявку, копируя кабинет и отдел из профиля на текущий момент.

    Копия, а не ссылка: сотрудник переедет, а заявка должна помнить, куда тогда
    шёл исполнитель.

    Маскирование выполняется здесь, а не доверяется вызывающему. Требование «сырой
    текст не попадает в БД» — главное в проекте, и оно должно держаться на
    конструкции, а не на памяти того, кто напишет следующий хендлер. Хендлеру
    вызывать `redact()` не нужно: `mask()` переживает повторный прогон вместе с
    флагом.
    """
    masked_text, security_flag = mask(text)

    ticket = Ticket(
        requester_id=employee.telegram_id,
        text=masked_text,
        status=TicketStatus.NEW,
        room_snapshot=employee.room,
        department_snapshot=employee.department,
        security_flag=security_flag,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        text_hash=text_hash(masked_text),
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

    Все три поля пишутся одним UPDATE, а `taken_at` вычисляется через COALESCE в
    самой БД. Через ORM-атрибуты это было бы хрупко: присвоение того же значения,
    что уже лежит в снимке сессии, не помечает поле изменённым, и оно молча
    выпадает из UPDATE. Тогда исход зависел бы от состояния кеша сессии — а два
    нажатия кнопок по одной заявке дали бы статус «в работе» с датой закрытия.
    """
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        logger.warning("Попытка сменить статус несуществующей заявки #%s", ticket_id)
        return None

    now = utcnow()
    taken_at = (
        func.coalesce(Ticket.taken_at, now)
        if status is TicketStatus.IN_PROGRESS
        else Ticket.taken_at
    )

    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id)
        .values(
            status=status,
            taken_at=taken_at,
            closed_at=now if status in CLOSED_STATUSES else None,
        )
    )
    await session.commit()

    if result.rowcount == 0:
        # Заявка исчезла между чтением и записью. `refresh()` на такой строке
        # бросает InvalidRequestError, поэтому проверяем до него.
        logger.warning("Заявка #%s исчезла во время смены статуса", ticket_id)
        session.expunge(ticket)
        return None

    await session.refresh(ticket)
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

    Текст маскируется перед хешированием, потому что в БД лежат хеши от
    замаскированного. Без этого повтор обращения с паролем внутри никогда бы не
    совпал сам с собой, и дедупликация молча отключалась бы именно на тех
    обращениях, где заявителю важнее всего получить один ответ, а не два.
    """
    masked_text, _ = mask(text)

    statement = (
        select(Ticket)
        .where(
            Ticket.requester_id == requester_id,
            Ticket.text_hash == text_hash(masked_text),
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
    """Добавляет реплику в переписку по заявке (F8).

    Маскируется здесь по той же причине, что и текст заявки: ответ сотрудника —
    такой же непроверенный ввод, и пароль он присылает в него не реже.
    """
    masked_text, _ = mask(text)
    message = TicketMessage(ticket_id=ticket_id, direction=direction, text=masked_text)
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
