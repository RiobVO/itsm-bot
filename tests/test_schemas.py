"""Тесты контракта ответа модели.

Проверяется не Pydantic, а наши решения: где сломанный ответ отклоняется (и уходит
в retry), а где нормализуется, чтобы обращение не потерялось из-за косметики.
"""

import pytest
from pydantic import ValidationError

from itsm_bot.schemas import Action, Category, IntakeResult, Priority, Ticket, TicketType


def make_ticket(**overrides) -> dict:
    ticket = {
        "title": "1С: ошибка соединения с сервером",
        "description": "С утра не открывается 1С у всей бухгалтерии.",
        "type": "incident",
        "category": "erp_1c",
        "subcategory": "недоступен сервер 1С",
        "priority": "P1",
        "priority_reason": "Отдел целиком не может работать",
        "requester_display_name": "Иванов Пётр",
        "requester_telegram_id": "584112903",
        "department": "Бухгалтерия",
        "location": "3 этаж",
        "affected_object": "1С:Предприятие",
        "needs_clarification": [],
        "security_flag": False,
        "confidence": 0.93,
    }
    ticket.update(overrides)
    return ticket


class TestTicket:
    def test_valid_ticket_parses(self):
        ticket = Ticket.model_validate(make_ticket())

        assert ticket.type is TicketType.INCIDENT
        assert ticket.category is Category.ERP_1C
        assert ticket.priority is Priority.P1

    def test_unknown_category_is_rejected(self):
        with pytest.raises(ValidationError):
            Ticket.model_validate(make_ticket(category="cctv"))

    def test_long_title_is_truncated_not_rejected(self):
        ticket = Ticket.model_validate(make_ticket(title="Принтер " * 20))

        assert len(ticket.title) <= 80

    def test_confidence_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError):
            Ticket.model_validate(make_ticket(confidence=1.5))

    def test_extra_field_is_rejected(self):
        with pytest.raises(ValidationError):
            Ticket.model_validate(make_ticket(assignee="admin"))

    def test_nullable_fields_accept_none(self):
        ticket = Ticket.model_validate(
            make_ticket(department=None, location=None, affected_object=None, subcategory=None)
        )

        assert ticket.department is None


class TestIntakeResult:
    def test_create_ticket_branch(self):
        result = IntakeResult.model_validate(
            {
                "action": "create_ticket",
                "ticket": make_ticket(),
                "questions": None,
                "reply_to_user": "Заявка зарегистрирована как P1.",
            }
        )

        assert result.action is Action.CREATE_TICKET
        assert result.ticket is not None

    def test_create_ticket_without_ticket_is_rejected(self):
        with pytest.raises(ValidationError):
            IntakeResult.model_validate(
                {
                    "action": "create_ticket",
                    "ticket": None,
                    "questions": None,
                    "reply_to_user": "Заявка зарегистрирована.",
                }
            )

    def test_ask_clarification_branch(self):
        result = IntakeResult.model_validate(
            {
                "action": "ask_clarification",
                "ticket": None,
                "questions": ["Что перестало работать?", "Какая ошибка на экране?"],
                "reply_to_user": "Уточните два момента.",
            }
        )

        assert len(result.questions) == 2

    def test_ask_clarification_without_questions_is_rejected(self):
        with pytest.raises(ValidationError):
            IntakeResult.model_validate(
                {
                    "action": "ask_clarification",
                    "ticket": None,
                    "questions": [],
                    "reply_to_user": "Уточните.",
                }
            )

    def test_extra_questions_are_trimmed_to_limit(self):
        result = IntakeResult.model_validate(
            {
                "action": "ask_clarification",
                "ticket": None,
                "questions": ["Первый?", "Второй?", "Третий?"],
                "reply_to_user": "Уточните.",
            }
        )

        assert result.questions == ["Первый?", "Второй?"]

    def test_reject_branch_clears_payload(self):
        result = IntakeResult.model_validate(
            {
                "action": "reject",
                "ticket": make_ticket(),
                "questions": ["Лишний вопрос?"],
                "reply_to_user": "Я принимаю только заявки в поддержку.",
            }
        )

        assert result.ticket is None
        assert result.questions is None

    def test_empty_reply_is_rejected(self):
        # Пустой ответ означает, что сотрудник не получит ничего — это сломанный ответ.
        with pytest.raises(ValidationError):
            IntakeResult.model_validate(
                {
                    "action": "reject",
                    "ticket": None,
                    "questions": None,
                    "reply_to_user": "   ",
                }
            )


class TestJsonSchema:
    def test_schema_forbids_extra_properties(self):
        # Structured outputs требуют additionalProperties: false на каждом объекте.
        schema = IntakeResult.model_json_schema()

        assert schema["additionalProperties"] is False
        assert schema["$defs"]["Ticket"]["additionalProperties"] is False

    def test_all_fields_are_required(self):
        # В промте зафиксировано: отсутствующее значение — null, а не пропуск ключа.
        schema = IntakeResult.model_json_schema()
        ticket_schema = schema["$defs"]["Ticket"]

        assert set(schema["required"]) == {"action", "ticket", "questions", "reply_to_user"}
        assert set(ticket_schema["required"]) == set(ticket_schema["properties"])
