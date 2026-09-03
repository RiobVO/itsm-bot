"""Схема БД. Соответствует разделу 3 TODO.md.

Все отметки времени хранятся в UTC: бот живёт на ВМ, часовой пояс которой может
смениться, а разница времён между заявками должна остаться верной.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время в UTC. Вынесено отдельно, чтобы тесты могли подменить."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Время, которое остаётся UTC-aware после обратного чтения.

    SQLite хранит дату строкой и часовой пояс теряет: записанное aware-значение
    возвращается наивным, и сравнение с `datetime.now(UTC)` падает с TypeError.
    Приводим к UTC на записи и навешиваем tzinfo на чтении, чтобы наивное время
    не расползлось по коду.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Время должно быть timezone-aware, иначе UTC не гарантирован")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class TicketStatus(StrEnum):
    """Жизненный цикл заявки при одном исполнителе (решение D8)."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class MessageDirection(StrEnum):
    TO_REQUESTER = "to_requester"
    FROM_REQUESTER = "from_requester"


class Employee(Base):
    """Профиль сотрудника: кабинет и отдел спрашиваются один раз (решение D2).

    Строка создаётся только после полного сбора профиля — заявка без кабинета
    исполнителю бесполезна, он по этому кабинету идёт.
    """

    __tablename__ = "employees"

    telegram_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    room: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )

    tickets: Mapped[list[Ticket]] = relationship(back_populates="requester")


class Ticket(Base):
    """Заявка. Номер `id` называют вслух («что там с 47-й»), поэтому автоинкремент."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("employees.telegram_id", ondelete="RESTRICT"), index=True
    )

    text: Mapped[str] = mapped_column(Text)
    """Замаскированный текст, склеенный из пачки сообщений (F5)."""

    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, native_enum=False, length=16, validate_strings=True),
        default=TicketStatus.NEW,
    )

    room_snapshot: Mapped[str] = mapped_column(String(64))
    department_snapshot: Mapped[str] = mapped_column(String(128))
    """Кабинет и отдел на момент заявки: люди переезжают, а отчёт за прошлый год
    должен помнить, куда тогда ходили."""

    security_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    source_chat_id: Mapped[int] = mapped_column(Integer)
    source_message_id: Mapped[int] = mapped_column(Integer)
    """Первое сообщение пачки — на него отвечаем заявителю."""

    queue_message_id: Mapped[int | None] = mapped_column(Integer)
    """Карточка в канале исполнителя; нужен, чтобы перерисовывать кнопки."""

    text_hash: Mapped[str] = mapped_column(String(64))
    """Для дедупликации (N11)."""

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, index=True
    )
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    requester: Mapped[Employee] = relationship(back_populates="tickets")
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_tickets_requester_status", "requester_id", "status"),
        Index("ix_tickets_dedup", "requester_id", "text_hash", "created_at"),
        CheckConstraint(
            "closed_at IS NULL OR created_at <= closed_at",
            name="ck_tickets_closed_after_created",
        ),
    )


class TicketMessage(Base):
    """Переписка по заявке: вопрос исполнителя и ответ сотрудника (F8)."""

    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, native_enum=False, length=16, validate_strings=True)
    )
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
