"""Тесты обработки собранной пачки.

Главное здесь — сериализация на `telegram_id`: маршрутизация, проверка дубля,
рейт-лимит и вставка обязаны идти под одной блокировкой (раздел 6 TODO.md).
Тесты гонки детерминированы барьером, а не надеждой на планировщик: `asyncio.gather`
сам по себе не заставляет обе задачи дойти до проверки дубля раньше первой вставки.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_bot.bot.buffer import Batch, MessageBuffer
from itsm_bot.bot.intake import Intake, Outcome
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language, MessageDirection, Ticket

REQUESTER = "584112903"


async def _profile(session: AsyncSession, telegram_id: str = REQUESTER) -> None:
    await repo.save_employee(
        session,
        telegram_id=telegram_id,
        display_name="Иванов Пётр",
        room="214",
        department="Бухгалтерия",
        language=Language.RU,
    )


def _batch(text: str = "Принтер не печатает", telegram_id: str = REQUESTER) -> Batch:
    return Batch(telegram_id=telegram_id, text=text, chat_id=7, first_message_id=100)


@pytest.fixture
def published() -> list[int]:
    return []


@pytest.fixture
def buffer() -> MessageBuffer:
    async def never(batch: Batch) -> None:
        raise AssertionError("буфер в этих тестах не используется для доставки")

    return MessageBuffer(window_seconds=10, flush=never)


@pytest.fixture
def intake(
    session_factory: async_sessionmaker[AsyncSession],
    buffer: MessageBuffer,
    published: list[int],
) -> Intake:
    async def publish(ticket_id: int) -> int | None:
        published.append(ticket_id)
        return 5000 + ticket_id

    return Intake(
        session_factory=session_factory,
        buffer=buffer,
        publish=publish,
        rate_limit_per_hour=5,
        dedup_window_minutes=10,
    )


async def _count_tickets(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(Ticket.id)))).scalar_one()


class TestCreate:
    async def test_ticket_is_created_and_published(
        self, session: AsyncSession, intake: Intake, published: list[int]
    ) -> None:
        await _profile(session)

        result = await intake.process(_batch())

        assert result.outcome is Outcome.CREATED
        assert published == [result.ticket_id]

    async def test_queue_message_id_is_saved(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """Без этого кнопки статуса потом некуда перерисовывать, а F8 теряет привязку."""
        await _profile(session)

        result = await intake.process(_batch())

        assert result.ticket_id is not None
        ticket = await repo.get_ticket(session, result.ticket_id)
        assert ticket is not None
        assert ticket.queue_message_id == 5000 + result.ticket_id

    async def test_secret_is_reported_to_the_caller(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """F10: заявка создаётся, а сотруднику нужно отдать предупреждение."""
        await _profile(session)

        result = await intake.process(_batch(text="мой пароль qwerty123"))

        assert result.outcome is Outcome.CREATED
        assert result.security_flag is True

    async def test_without_profile_nothing_is_created(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        result = await intake.process(_batch())

        assert result.outcome is Outcome.NO_PROFILE
        assert await _count_tickets(session) == 0

    async def test_failed_publish_is_reported_but_ticket_survives(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        buffer: MessageBuffer,
    ) -> None:
        """Заявка уже в БД: потерять её хуже, чем остаться без карточки."""
        await _profile(session)

        async def failing_publish(ticket_id: int) -> int | None:
            return None

        intake = Intake(
            session_factory=session_factory,
            buffer=buffer,
            publish=failing_publish,
            rate_limit_per_hour=5,
            dedup_window_minutes=10,
        )

        result = await intake.process(_batch())

        assert result.outcome is Outcome.CREATED
        assert result.published is False
        assert await _count_tickets(session) == 1


class TestAnswer:
    async def test_batch_goes_to_the_waiting_ticket(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """D15: пока заявка ждёт ответа, пачка идёт в переписку, а не в новую заявку."""
        await _profile(session)
        created = await intake.process(_batch(text="Принтер"))
        assert created.ticket_id is not None
        await repo.add_message(
            session, created.ticket_id, MessageDirection.TO_REQUESTER, "Какая модель?"
        )

        result = await intake.process(_batch(text="HP M404"))

        assert result.outcome is Outcome.ANSWERED
        assert result.ticket_id == created.ticket_id
        assert await _count_tickets(session) == 1

    async def test_multipart_answer_stays_in_one_ticket(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """Ответ пишут очередью так же, как обращение (D7): «HP M404» → «ошибка 50.4».

        Решение принимается на склеенной пачке, поэтому оба сообщения — один ответ.
        """
        await _profile(session)
        created = await intake.process(_batch(text="Принтер"))
        assert created.ticket_id is not None
        await repo.add_message(
            session, created.ticket_id, MessageDirection.TO_REQUESTER, "Какая модель?"
        )

        await intake.process(_batch(text="HP M404\nошибка 50.4"))

        assert await _count_tickets(session) == 1
        messages = await repo.ticket_messages(session, created.ticket_id)
        assert [item.direction for item in messages] == [
            MessageDirection.TO_REQUESTER,
            MessageDirection.FROM_REQUESTER,
        ]


class TestDuplicate:
    async def test_same_text_within_window_does_not_create_second(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """N11."""
        await _profile(session)
        first = await intake.process(_batch())

        second = await intake.process(_batch())

        assert second.outcome is Outcome.DUPLICATE
        assert second.ticket_id == first.ticket_id
        assert await _count_tickets(session) == 1

    async def test_same_text_from_another_employee_is_not_a_duplicate(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        """Про один сломанный принтер пишут несколько человек — это разные обращения."""
        await _profile(session)
        await _profile(session, telegram_id="999")
        await intake.process(_batch())

        result = await intake.process(_batch(telegram_id="999"))

        assert result.outcome is Outcome.CREATED
        assert await _count_tickets(session) == 2


class TestRateLimit:
    async def test_limit_stops_creation(self, session: AsyncSession, intake: Intake) -> None:
        """N10: пять заявок в час."""
        await _profile(session)
        for index in range(5):
            await intake.process(_batch(text=f"Обращение {index}"))

        result = await intake.process(_batch(text="Шестое"))

        assert result.outcome is Outcome.RATE_LIMITED
        assert await _count_tickets(session) == 5


class TestSerialization:
    """Требование ревью этапа 1: проверка дубля и вставка идут под одной блокировкой."""

    async def test_two_concurrent_batches_create_one_ticket(
        self,
        session: AsyncSession,
        intake: Intake,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Барьер удерживает первую вставку, пока вторая задача не прочитает БД.

        Событие взводится ПОСЛЕ завершения второго запроса дубля. Взведённое до
        него, оно отпускало бы первую вставку слишком рано: та успевала
        закоммититься, вторая видела дубль, и тест оставался зелёным на реализации
        без блокировки.
        """
        await _profile(session)
        original_duplicate = repo.find_recent_duplicate
        original_create = repo.create_ticket
        second_reached_dedup = asyncio.Event()
        dedup_calls = 0

        async def counting_duplicate(*args: object, **kwargs: object):
            nonlocal dedup_calls
            dedup_calls += 1
            result = await original_duplicate(*args, **kwargs)  # type: ignore[arg-type]
            if dedup_calls == 2:
                second_reached_dedup.set()
            return result

        async def blocked_create(*args: object, **kwargs: object):
            try:
                await asyncio.wait_for(second_reached_dedup.wait(), timeout=0.5)
            except TimeoutError:
                pass
            return await original_create(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(repo, "find_recent_duplicate", counting_duplicate)
        monkeypatch.setattr(repo, "create_ticket", blocked_create)

        results = await asyncio.gather(
            intake.process(_batch()), intake.process(_batch())
        )

        assert await _count_tickets(session) == 1
        assert {item.outcome for item in results} == {Outcome.CREATED, Outcome.DUPLICATE}

    async def test_concurrent_batches_do_not_exceed_rate_limit(
        self, session: AsyncSession, intake: Intake
    ) -> None:
        await _profile(session)

        await asyncio.gather(
            *(intake.process(_batch(text=f"Обращение {index}")) for index in range(8))
        )

        assert await _count_tickets(session) == 5

    async def test_one_employee_does_not_block_another(
        self, session: AsyncSession, intake: Intake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Блокировка обязана быть на сотруднике, а не общей.

        С одной глобальной блокировкой обе заявки тоже создадутся, и проверка по их
        числу была бы зелёной на неверной реализации — поэтому сверяется порядок.
        """
        await _profile(session)
        await _profile(session, telegram_id="999")
        original_create = repo.create_ticket
        order: list[str] = []

        async def ordered_create(session_arg, *, employee, **kwargs):  # type: ignore[no-untyped-def]
            if employee.telegram_id == REQUESTER:
                await asyncio.sleep(0.1)
            order.append(employee.telegram_id)
            return await original_create(session_arg, employee=employee, **kwargs)

        monkeypatch.setattr(repo, "create_ticket", ordered_create)

        await asyncio.gather(
            intake.process(_batch(text="принтер")),
            intake.process(_batch(text="мышь", telegram_id="999")),
        )

        assert order == ["999", REQUESTER]


class TestBufferChain:
    """Связка буфер → flush → intake целиком.

    Отдельные тесты обоих модулей были зелёными при дедлоке: буфер проверялся с
    обработчиком, который блокировку не берёт, а intake вызывался мимо буфера.
    """

    async def test_batch_from_buffer_becomes_a_ticket(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _profile(session)
        holder: dict[str, Intake] = {}

        async def flush(batch: Batch) -> None:
            await holder["intake"].process(batch)

        async def publish(ticket_id: int) -> int | None:
            return 1

        buffer = MessageBuffer(window_seconds=0.05, flush=flush)
        holder["intake"] = Intake(
            session_factory=session_factory,
            buffer=buffer,
            publish=publish,
            rate_limit_per_hour=5,
            dedup_window_minutes=10,
        )

        await buffer.add(REQUESTER, text="Принтер", chat_id=7, message_id=100)

        async def wait_for_ticket() -> None:
            while await _count_tickets(session) == 0:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_ticket(), timeout=3)

    async def test_release_after_survey_becomes_a_ticket(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Путь F1: обращение ждало анкеты в удержанном буфере."""
        holder: dict[str, Intake] = {}

        async def flush(batch: Batch) -> None:
            await holder["intake"].process(batch)

        async def publish(ticket_id: int) -> int | None:
            return 1

        buffer = MessageBuffer(window_seconds=10, flush=flush)
        holder["intake"] = Intake(
            session_factory=session_factory,
            buffer=buffer,
            publish=publish,
            rate_limit_per_hour=5,
            dedup_window_minutes=10,
        )

        buffer.hold(REQUESTER)
        await buffer.add(REQUESTER, text="Принтер", chat_id=7, message_id=100)
        await _profile(session)

        await asyncio.wait_for(buffer.release(REQUESTER), timeout=3)

        assert await _count_tickets(session) == 1
