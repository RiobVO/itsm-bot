"""Тесты операций над заявками.

Проверяют поведение, описанное требованиями F1-F11 и N10-N11 в TODO.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.storage import repo
from itsm_bot.storage.models import MessageDirection, TicketStatus

REQUESTER = "584112903"


async def _employee(session: AsyncSession, telegram_id: str = REQUESTER):
    return await repo.save_employee(
        session,
        telegram_id=telegram_id,
        display_name="Иванов Пётр",
        room="214",
        department="Бухгалтерия",
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
        )

        found = await repo.get_employee(session, REQUESTER)
        assert found is not None
        assert found.room == "301"

    async def test_unknown_employee_is_none(self, session: AsyncSession) -> None:
        assert await repo.get_employee(session, "нет такого") is None


class TestCreateTicket:
    async def test_ticket_is_created_and_read_back(self, session: AsyncSession) -> None:
        employee = await _employee(session)

        ticket = await repo.create_ticket(
            session,
            employee=employee,
            text="Принтер не печатает",
            security_flag=False,
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
            security_flag=False,
            source_chat_id=1,
            source_message_id=10,
        )

        await repo.save_employee(
            session,
            telegram_id=REQUESTER,
            display_name="Иванов Пётр",
            room="999",
            department="Логистика",
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
            security_flag=False,
            source_chat_id=1,
            source_message_id=10,
        )
        second = await repo.create_ticket(
            session,
            employee=employee,
            text="Вторая",
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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

    async def test_same_text_from_another_employee_is_not_a_duplicate(
        self, session: AsyncSession
    ) -> None:
        """Про один и тот же сломанный принтер пишут разные люди — это разные заявки."""
        employee = await _employee(session)
        await repo.create_ticket(
            session,
            employee=employee,
            text="Не печатает принтер",
            security_flag=False,
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
            security_flag=False,
            source_chat_id=1,
            source_message_id=10,
        )
        old = await repo.create_ticket(
            session,
            employee=employee,
            text="Старая",
            security_flag=False,
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
            security_flag=False,
            source_chat_id=1,
            source_message_id=10,
        )
        closed = await repo.create_ticket(
            session,
            employee=employee,
            text="Закрытая",
            security_flag=False,
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
            security_flag=False,
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
            security_flag=False,
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
