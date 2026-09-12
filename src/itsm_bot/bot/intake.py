"""Обработка собранной пачки — единственная точка создания заявки на весь бот.

Маршрутизация, проверка дубля (N11), рейт-лимит (N10) и вставка идут под одной
блокировкой на `telegram_id`. По отдельности они не помогают: две параллельные
обработки одного человека обе не увидят дубля и обе насчитают 4 заявки из 5.

Блокировки хватает ровно потому, что пишущий процесс один (D4). Появится второй —
сериализацию придётся переносить в БД; это записано в условии пересмотра D4.

Решение «ответ или новая заявка» принимается здесь, на собранной пачке, а не в
хендлере на каждом сообщении: ответ пишут очередью так же, как обращение (D7), и
решение на каждом сообщении разорвало бы его на ответ и заявку.

Отправка карточки передаётся параметром `publish`, а не импортируется: так модуль
тестируется без работающего бота.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_bot.bot import routing
from itsm_bot.bot.buffer import Batch, MessageBuffer
from itsm_bot.storage import repo
from itsm_bot.storage.models import Employee, MessageDirection

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"
    NO_PROFILE = "no_profile"
    ANSWERED = "answered"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    ticket_id: int | None = None
    security_flag: bool = False
    published: bool = True


@dataclass
class Intake:
    session_factory: async_sessionmaker[AsyncSession]
    buffer: MessageBuffer
    publish: Callable[[int], Awaitable[int | None]]
    rate_limit_per_hour: int
    dedup_window_minutes: int

    async def process(self, batch: Batch) -> Result:
        async with self.buffer.lock(batch.telegram_id):
            async with self.session_factory() as session:
                return await self._process(session, batch)

    async def _process(self, session: AsyncSession, batch: Batch) -> Result:
        employee = await repo.get_employee(session, batch.telegram_id)
        if employee is None:
            # Анкета обязана заполниться раньше: заявка без кабинета исполнителю
            # бесполезна, он по этому кабинету идёт.
            logger.warning("Обращение без профиля от %s", batch.telegram_id)
            return Result(Outcome.NO_PROFILE)

        decision = await routing.decide(
            session, requester_id=batch.telegram_id, asked_before=batch.started_at
        )
        if decision.kind is routing.Kind.ANSWER and decision.ticket_id is not None:
            return await self._answer(session, decision.ticket_id, batch)

        return await self._create(session, employee, batch)

    async def _answer(
        self, session: AsyncSession, ticket_id: int, batch: Batch
    ) -> Result:
        await repo.add_message(
            session, ticket_id, MessageDirection.FROM_REQUESTER, batch.text
        )
        logger.info(
            "Ответ сотрудника %s записан в заявку #%s", batch.telegram_id, ticket_id
        )
        return Result(Outcome.ANSWERED, ticket_id=ticket_id)

    async def _create(
        self, session: AsyncSession, employee: Employee, batch: Batch
    ) -> Result:
        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id=batch.telegram_id,
            text=batch.text,
            window=timedelta(minutes=self.dedup_window_minutes),
        )
        if duplicate is not None:
            logger.info(
                "Повтор обращения от %s, уже заведено #%s",
                batch.telegram_id,
                duplicate.id,
            )
            return Result(Outcome.DUPLICATE, ticket_id=duplicate.id)

        recent = await repo.count_recent_tickets(
            session, requester_id=batch.telegram_id, window=timedelta(hours=1)
        )
        if recent >= self.rate_limit_per_hour:
            logger.info("Рейт-лимит у %s: %s заявок за час", batch.telegram_id, recent)
            return Result(Outcome.RATE_LIMITED)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text=batch.text,
            source_chat_id=batch.chat_id,
            source_message_id=batch.first_message_id,
        )

        queue_message_id = await self.publish(ticket.id)
        if queue_message_id is not None:
            await repo.set_queue_message_id(session, ticket.id, queue_message_id)
        else:
            # Заявка уже в БД, и потерять её хуже, чем остаться без карточки.
            # Карточка досылается вручную через /publish и видна в /queue.
            logger.error("Заявка #%s создана без карточки в очереди", ticket.id)

        return Result(
            Outcome.CREATED,
            ticket_id=ticket.id,
            security_flag=ticket.security_flag,
            published=queue_message_id is not None,
        )
