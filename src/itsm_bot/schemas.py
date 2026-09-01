"""Контракт обмена с моделью.

Одна и та же модель служит тремя вещами: JSON Schema для structured outputs,
валидацией ответа и источником полей для записи в БД.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 80
MAX_CLARIFICATION_QUESTIONS = 2


class Action(StrEnum):
    """Ветка решения: заявка, уточнение или отказ."""

    CREATE_TICKET = "create_ticket"
    ASK_CLARIFICATION = "ask_clarification"
    REJECT = "reject"


class TicketType(StrEnum):
    INCIDENT = "incident"
    SERVICE_REQUEST = "service_request"
    CHANGE = "change"
    QUESTION = "question"


class Category(StrEnum):
    """Закрытый справочник. Значение вне списка — сломанный ответ, не новая категория."""

    ACCESS = "access"
    NETWORK = "network"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    PRINTING = "printing"
    EMAIL = "email"
    ERP_1C = "erp_1c"
    TELEPHONY = "telephony"
    OTHER = "other"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Requester(BaseModel):
    """Метаданные отправителя. Подставляются в <requester> и копируются моделью дословно.

    department берётся из справочника сотрудников, которого пока нет — до его появления
    поле остаётся пустым, и модель обязана вернуть по нему null.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None
    telegram_id: str
    department: str | None


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    type: TicketType
    category: Category
    subcategory: str | None
    priority: Priority
    priority_reason: str
    requester_display_name: str | None
    requester_telegram_id: str | None
    department: str | None
    location: str | None
    affected_object: str | None
    needs_clarification: list[str]
    security_flag: bool
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("title")
    @classmethod
    def _truncate_title(cls, value: str) -> str:
        """Длина заголовка — косметика: терять из-за неё заявку нельзя."""
        if len(value) <= TITLE_MAX_LENGTH:
            return value
        logger.warning("Заголовок длиннее %d символов, обрезан", TITLE_MAX_LENGTH)
        return value[:TITLE_MAX_LENGTH].rstrip()


class IntakeResult(BaseModel):
    """Единственный ответ модели на одно обращение."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    ticket: Ticket | None
    questions: list[str] | None
    reply_to_user: str

    @model_validator(mode="after")
    def _enforce_branch_contract(self) -> IntakeResult:
        """Сверяет ветку с полезной нагрузкой.

        Несоответствие ветки и данных означает сломанный ответ — его отклоняем, чтобы
        вызывающий код ушёл в retry. Избыточные данные внутри корректной ветки просто
        отбрасываем: ради них терять обращение незачем.
        """
        if not self.reply_to_user.strip():
            raise ValueError("reply_to_user не может быть пустым: сотрудник останется без ответа")

        match self.action:
            case Action.CREATE_TICKET:
                if self.ticket is None:
                    raise ValueError("action=create_ticket требует заполненный ticket")
                self.questions = None
            case Action.ASK_CLARIFICATION:
                if not self.questions:
                    raise ValueError("action=ask_clarification требует хотя бы один вопрос")
                self.questions = self.questions[:MAX_CLARIFICATION_QUESTIONS]
                self.ticket = None
            case Action.REJECT:
                self.ticket = None
                self.questions = None

        return self
