"""Тесты операций над заявками.

Проверяют поведение, описанное требованиями F1-F11 и N10-N11 в TODO.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.storage import repo
from itsm_bot.storage.models import Language, MessageDirection, TicketStatus

REQUESTER = "584112903"


async def _employee(
    session: AsyncSession,
    telegram_id: str = REQUESTER,
    language: Language = Language.RU,
):
    return await repo.save_employee(
        session,
        telegram_id=telegram_id,
        display_name="Иванов Пётр",
        room="214",
        department="Бухгалтерия",
        language=language,
    )


class TestEmployee:
    async def test_profile_is_created_and_read_back(self, session: AsyncSession) -> None:
        await _employee(session)

        found = await repo.get_employee(session, REQUESTER)

        assert found is not None
        assert found.room == "214"
        assert found.department == "Бухгалтерия"

    async def test_second_save_updates_instead_of_duplicating(
        self, session: AsyncSession
    ) -> None:
        """F3: /profile правит существующий профиль, а не заводит второй."""
        await _employee(session)

        await repo.save_employee(
            session,
            telegram_id=REQUESTER,
            display_name="Иванов Пётр",
            room="301",
            department="Бухгалтерия",
            language=Language.RU,
        )

        found = await repo.get_employee(session, REQUESTER)
        assert found is not None
        assert found.room == "301"

    async def test_unknown_employee_is_none(self, session: AsyncSession) -> None:
        assert await repo.get_employee(session, "нет такого") is None


class TestRoomMasking:
    async def test_secret_in_room_is_masked(self, session: AsyncSession) -> None:
        """Сбой анкеты превращает кабинет в приёмник произвольного текста (N8)."""
        await repo.save_employee(
            session,
            telegram_id=REQUESTER,
            display_name="Иванов Пётр",
            room="пароль qwerty123",
            department="Бухгалтерия",
            language=Language.RU,
        )

        found = await repo.get_employee(session, REQUESTER)

        assert found is not None
        assert "qwerty123" not in found.room

    async def test_ordinary_room_is_untouched(self, session: AsyncSession) -> None:
        await _employee(session)

        found = await repo.get_employee(session, REQUESTER)

        assert found is not None
        assert found.room == "214"


class TestLanguage:
    async def test_language_is_stored_and_read_back(self, session: AsyncSession) -> None:
        """D14: язык — свойство человека, уведомления F7 берут его отсюда."""
        await _employee(session, language=Language.EN)

        found = await repo.get_employee(session, REQUESTER)

        assert found is not None
        assert found.language is Language.EN

    async def test_language_is_stored_as_value_not_member_name(
        self, session: AsyncSession
    ) -> None:
        """Перечисление обязано лечь в БД значением — иначе `language = 'en'` не найдёт строку."""
        await _employee(session, language=Language.EN)

        stored = await session.execute(text("SELECT language FROM employees"))
        assert stored.scalar_one() == "en"


class TestCreateTicket:
    async def test_ticket_is_created_and_read_back(self, session: AsyncSession) -> None:
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Принтер не печатает",
            source_chat_id=1,
            source_message_id=10,
        )

        found = await repo.get_ticket(session, ticket.id)
        assert found is not None
        assert found.text == "Принтер не печатает"
        assert found.status is TicketStatus.NEW
        assert found.taken_at is None

    async def test_location_is_snapshotted_from_profile(
        self, session: AsyncSession
    ) -> None:
        """Заявка помнит кабинет на свой момент — сотрудник может переехать позже."""
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Не работает телефон",
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.save_employee(
            session,
            telegram_id=REQUESTER,
            display_name="Иванов Пётр",
            room="999",
            department="Логистика",
            language=Language.RU,
        )

        found = await repo.get_ticket(session, ticket.id)
        assert found is not None
        assert found.room_snapshot == "214"
        assert found.department_snapshot == "Бухгалтерия"

    async def test_numbers_are_sequential(self, session: AsyncSession) -> None:
        """Номер называют вслух, поэтому он растёт по порядку."""
        employee = await _employee(session)
        first = await repo.create_ticket(
            session,
            employee=employee,
            text="Первая",
            source_chat_id=1,
            source_message_id=10,
        )
        second = await repo.create_ticket(
            session,
            employee=employee,
            text="Вторая",
            source_chat_id=1,
            source_message_id=11,
        )

        assert second.id == first.id + 1

    async def test_ticket_without_employee_is_rejected(
        self, session: AsyncSession
    ) -> None:
        """Внешние ключи в SQLite включаются вручную — проверяем, что включены."""
        from itsm_bot.storage.models import Ticket

        session.add(
            Ticket(
                requester_id="никому не известный",
                text="текст",
                room_snapshot="1",
                department_snapshot="отдел",
                source_chat_id=1,
                source_message_id=1,
                text_hash="deadbeef",
            )
        )

        with pytest.raises(IntegrityError):
            await session.commit()


class TestStatus:
    async def test_taking_into_work_sets_timestamp(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        updated = await repo.set_status(session, ticket.id, TicketStatus.IN_PROGRESS)

        assert updated is not None
        assert updated.taken_at is not None
        assert updated.closed_at is None

    async def test_closing_sets_closed_at(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        updated = await repo.set_status(session, ticket.id, TicketStatus.DONE)

        assert updated is not None
        assert updated.closed_at is not None

    async def test_reopening_clears_closed_at(self, session: AsyncSession) -> None:
        """Иначе заявка останется закрытой по времени, но открытой по статусу."""
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )
        await repo.set_status(session, ticket.id, TicketStatus.DONE)

        updated = await repo.set_status(session, ticket.id, TicketStatus.IN_PROGRESS)

        assert updated is not None
        assert updated.closed_at is None

    async def test_unknown_ticket_returns_none(self, session: AsyncSession) -> None:
        assert await repo.set_status(session, 4242, TicketStatus.DONE) is None


class TestDeduplication:
    """N11: одинаковый текст от одного человека за окно не заводит вторую заявку."""

    async def test_same_text_within_window_is_found(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер",
            source_chat_id=1,
            source_message_id=10,
        )

        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id=REQUESTER,
            text="Не печатает принтер",
            window=timedelta(minutes=10),
        )

        assert duplicate is not None

    async def test_older_than_window_is_not_a_duplicate(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер",
            source_chat_id=1,
            source_message_id=10,
        )
        ticket.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id=REQUESTER,
            text="Не печатает принтер",
            window=timedelta(minutes=10),
        )

        assert duplicate is None

    async def test_different_text_is_not_a_duplicate(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер",
            source_chat_id=1,
            source_message_id=10,
        )

        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id=REQUESTER,
            text="Не работает телефон",
            window=timedelta(minutes=10),
        )

        assert duplicate is None

    async def test_duplicate_is_found_in_text_containing_a_secret(
        self, session: AsyncSession
    ) -> None:
        """В БД лежит хеш замаскированного текста — искать надо по такому же.

        Иначе дедупликация молча отключается именно на обращениях с паролем, где
        заявителю особенно не нужен второй ответ на то же самое.
        """
        employee = await _employee(session)
        raw = "не пускает почта, пароль: Zavod2024!"
        await repo.create_ticket(
            session,
            employee=employee,
            text=raw,
            source_chat_id=1,
            source_message_id=10,
        )

        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id=REQUESTER,
            text=raw,
            window=timedelta(minutes=10),
        )

        assert duplicate is not None

    async def test_same_text_from_another_employee_is_not_a_duplicate(
        self, session: AsyncSession
    ) -> None:
        """Про один и тот же сломанный принтер пишут разные люди — это разные заявки."""
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер",
            source_chat_id=1,
            source_message_id=10,
        )
        await _employee(session, telegram_id="777")

        duplicate = await repo.find_recent_duplicate(
            session,
            requester_id="777",
            text="Не печатает принтер",
            window=timedelta(minutes=10),
        )

        assert duplicate is None


class TestRateLimit:
    """N10: не больше 5 заявок в час от одного сотрудника."""

    async def test_counts_only_last_hour(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        recent = await repo.create_ticket(
            session,
            employee=employee,
            text="Свежая",
            source_chat_id=1,
            source_message_id=10,
        )
        old = await repo.create_ticket(
            session,
            employee=employee,
            text="Старая",
            source_chat_id=1,
            source_message_id=11,
        )
        old.created_at = datetime.now(UTC) - timedelta(hours=3)
        await session.commit()

        count = await repo.count_recent_tickets(
            session, requester_id=REQUESTER, window=timedelta(hours=1)
        )

        assert count == 1
        assert recent.id != old.id


class TestOpenTickets:
    """F9: /my показывает только незакрытые заявки."""

    async def test_closed_tickets_are_excluded(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        open_ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Открытая",
            source_chat_id=1,
            source_message_id=10,
        )
        closed = await repo.create_ticket(
            session,
            employee=employee,
            text="Закрытая",
            source_chat_id=1,
            source_message_id=11,
        )
        await repo.set_status(session, closed.id, TicketStatus.DONE)

        found = await repo.open_tickets(session, requester_id=REQUESTER)

        assert [ticket.id for ticket in found] == [open_ticket.id]

    async def test_other_employees_tickets_are_excluded(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Чужая",
            source_chat_id=1,
            source_message_id=10,
        )
        await _employee(session, telegram_id="777")

        assert await repo.open_tickets(session, requester_id="777") == []


class TestTicketMessages:
    """F8: вопрос исполнителя и ответ сотрудника привязаны к заявке."""

    async def test_question_and_answer_are_stored_in_order(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.add_message(
            session, ticket.id, MessageDirection.TO_REQUESTER, "Какой это кабинет?"
        )
        await repo.add_message(
            session, ticket.id, MessageDirection.FROM_REQUESTER, "214"
        )

        messages = await repo.ticket_messages(session, ticket.id)

        assert [message.direction for message in messages] == [
            MessageDirection.TO_REQUESTER,
            MessageDirection.FROM_REQUESTER,
        ]
        assert messages[1].text == "214"


class TestRedactionIsEnforced:
    """N8: сырой текст не попадает в БД, чем бы ни был занят вызывающий.

    Маскирование встроено в слой хранения, поэтому забыть его нельзя.
    """

    async def test_secret_never_reaches_the_ticket(self, session: AsyncSession) -> None:
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Не заходит почта, пароль: Zavod2024!",
            source_chat_id=1,
            source_message_id=10,
        )

        stored = (await session.execute(text("SELECT text FROM tickets"))).scalar_one()
        assert "Zavod2024!" not in stored
        assert "[REDACTED:secret]" in stored
        assert ticket.security_flag is True

    async def test_flag_stays_false_without_secrets(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер в 214",
            source_chat_id=1,
            source_message_id=10,
        )

        assert ticket.security_flag is False
        assert ticket.text == "Не печатает принтер в 214"

    async def test_secret_never_reaches_a_reply(self, session: AsyncSession) -> None:
        """Ответ сотрудника — такой же непроверенный ввод, как и само обращение."""
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.add_message(
            session,
            ticket.id,
            MessageDirection.FROM_REQUESTER,
            "мой пароль Qwerty12345",
        )

        stored = (
            await session.execute(text("SELECT text FROM ticket_messages"))
        ).scalar_one()
        assert "Qwerty12345" not in stored

    async def test_flag_survives_text_masked_further_up_the_pipeline(
        self, session: AsyncSession
    ) -> None:
        """Текст идемпотентен, а находка — нет: второй `redact()` видит плейсхолдер.

        Если бы флаг брался прямо из второго прогона, заявка с уже замаскированным
        текстом сохранилась бы с `security_flag=False` и сотрудник не получил бы
        предупреждения.
        """
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="токен: [REDACTED:secret]",
            source_chat_id=1,
            source_message_id=10,
        )

        assert ticket.security_flag is True

    async def test_hash_is_built_from_masked_text(self, session: AsyncSession) -> None:
        """Иначе хеш сам стал бы оракулом: подбор по нему восстанавливает секрет."""
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="пароль: Zavod2024!",
            source_chat_id=1,
            source_message_id=10,
        )

        assert ticket.text_hash == repo.text_hash("пароль: [REDACTED:secret]")


class TestStoredEnumValues:
    """Значения перечислений в БД совпадают с контрактом из TODO.md.

    SQLAlchemy по умолчанию пишет имя члена (`IN_PROGRESS`), а не значение
    (`in_progress`), и документированный запрос `status = 'done'` не находит ничего.
    """

    async def test_status_is_stored_as_its_value(self, session: AsyncSession) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.set_status(session, ticket.id, TicketStatus.IN_PROGRESS)

        stored = (
            await session.execute(text("SELECT status FROM tickets"))
        ).scalar_one()
        assert stored == "in_progress"

    async def test_documented_query_finds_closed_tickets(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )
        await repo.set_status(session, ticket.id, TicketStatus.DONE)

        found = (
            await session.execute(
                text("SELECT count(*) FROM tickets WHERE status = 'done'")
            )
        ).scalar_one()
        assert found == 1

    async def test_direction_is_stored_as_its_value(
        self, session: AsyncSession
    ) -> None:
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.add_message(
            session, ticket.id, MessageDirection.TO_REQUESTER, "Вопрос"
        )

        stored = (
            await session.execute(text("SELECT direction FROM ticket_messages"))
        ).scalar_one()
        assert stored == "to_requester"

    async def test_invalid_status_is_rejected_by_the_database(
        self, session: AsyncSession
    ) -> None:
        """CHECK-констрейнт: опечатка в ручном UPDATE не создаёт несуществующий статус."""
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )

        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE tickets SET status = 'чушь'"))
            await session.commit()


class TestConcurrentStatusChanges:
    """Отметки времени не должны зависеть от того, что успела увидеть сессия."""

    async def test_reopening_clears_closure_made_in_another_session(
        self, session: AsyncSession, session_factory
    ) -> None:
        """Две кнопки по одной заявке не оставляют «в работе» с датой закрытия."""
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )
        ticket_id = ticket.id

        async with session_factory() as stale, session_factory() as closer:
            # Ссылку на объект нужно удерживать: identity map у SQLAlchemy слабая,
            # без живой ссылки снимок вытесняется, сессия перечитывает строку — и
            # тест проходит даже на реализации, которая теряет closed_at.
            held = await repo.get_ticket(stale, ticket_id)
            assert held is not None

            await repo.set_status(closer, ticket_id, TicketStatus.DONE)
            await repo.set_status(stale, ticket_id, TicketStatus.IN_PROGRESS)

            assert held is not None  # ссылка жива до конца блока

        async with session_factory() as check:
            final = await repo.get_ticket(check, ticket_id)
            assert final is not None
            assert final.status is TicketStatus.IN_PROGRESS
            assert final.closed_at is None

    async def test_vanished_ticket_returns_none_instead_of_raising(
        self, session: AsyncSession, session_factory
    ) -> None:
        """Заявка может исчезнуть между чтением и записью.

        `refresh()` на удалённой строке бросает InvalidRequestError — вызывающий
        получил бы исключение вместо понятного None.
        """
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )
        ticket_id = ticket.id

        async with session_factory() as victim, session_factory() as killer:
            held = await repo.get_ticket(victim, ticket_id)
            assert held is not None  # ссылку держим, иначе снимок вытеснится

            await killer.execute(
                text("DELETE FROM tickets WHERE id = :id"), {"id": ticket_id}
            )
            await killer.commit()

            assert await repo.set_status(victim, ticket_id, TicketStatus.DONE) is None
            assert held is not None

    async def test_taken_at_survives_a_second_take(
        self, session: AsyncSession, session_factory
    ) -> None:
        """Повторное «в работу» не сдвигает момент взятия — иначе метрика соврёт."""
        employee = await _employee(session)
        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Текст",
            source_chat_id=1,
            source_message_id=10,
        )
        ticket_id = ticket.id

        first = await repo.set_status(session, ticket_id, TicketStatus.IN_PROGRESS)
        assert first is not None
        original_taken_at = first.taken_at

        async with session_factory() as other:
            await repo.set_status(other, ticket_id, TicketStatus.IN_PROGRESS)

        async with session_factory() as check:
            final = await repo.get_ticket(check, ticket_id)
            assert final is not None
            assert final.taken_at == original_taken_at
