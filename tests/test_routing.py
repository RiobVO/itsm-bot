"""Тесты правила «ответ по заявке или новое обращение» (D15)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot.routing import Kind, decide
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language, MessageDirection, TicketStatus

REQUESTER = "584112903"


async def _ticket_with_question(session: AsyncSession, message_id: int):
    employee = await repo.get_employee(session, REQUESTER)
    if employee is None:
        employee = await repo.save_employee(
            session,
            telegram_id=REQUESTER,
            display_name="Иванов Пётр",
            room="214",
            department="Бухгалтерия",
            language=Language.RU,
        )
    ticket = await repo.create_ticket(
        session,
        employee=employee,
        text=f"Обращение {message_id}",
        source_chat_id=7,
        source_message_id=message_id,
    )
    await repo.add_message(session, ticket.id, MessageDirection.TO_REQUESTER, "Вопрос?")
    return ticket


class TestDecide:
    async def test_no_waiting_tickets_means_new_request(self, session: AsyncSession) -> None:
        decision = await decide(session, requester_id=REQUESTER)

        assert decision.kind is Kind.NEW
        assert decision.ticket_id is None

    async def test_single_waiting_ticket_takes_the_message(
        self, session: AsyncSession
    ) -> None:
        ticket = await _ticket_with_question(session, 100)

        decision = await decide(session, requester_id=REQUESTER)

        assert decision.kind is Kind.ANSWER
        assert decision.ticket_id == ticket.id

    async def test_second_waiting_ticket_never_appears(
        self, session: AsyncSession
    ) -> None:
        """Инвариант D15: неотвеченный вопрос у сотрудника ровно один.

        Второй вопрос не даёт задать хендлер очереди. Если он всё же появился —
        значит инвариант нарушен, и решение принимается по самой старой заявке, а в
        лог уходит предупреждение: молча подшить ответ к произвольной из двух хуже.
        """
        first = await _ticket_with_question(session, 100)
        await _ticket_with_question(session, 101)

        decision = await decide(session, requester_id=REQUESTER)

        assert decision.kind is Kind.ANSWER
        assert decision.ticket_id == first.id

    async def test_closed_ticket_does_not_catch_the_message(
        self, session: AsyncSession
    ) -> None:
        """D12."""
        ticket = await _ticket_with_question(session, 100)
        await repo.set_status(session, ticket.id, TicketStatus.DONE)

        decision = await decide(session, requester_id=REQUESTER)

        assert decision.kind is Kind.NEW
