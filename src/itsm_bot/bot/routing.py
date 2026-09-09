"""Куда отнести пачку сообщений: в открытую заявку или в новое обращение.

Правило детерминированное и всего из двух веток (D15). Двусмысленности не возникает
по построению: неотвеченный вопрос у сотрудника ровно один — второй вопрос тому же
человеку хендлер очереди задать не даёт.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.storage import repo

logger = logging.getLogger(__name__)


class Kind(StrEnum):
    ANSWER = "answer"
    NEW = "new"


@dataclass(frozen=True)
class Decision:
    kind: Kind
    ticket_id: int | None = None


async def decide(session: AsyncSession, *, requester_id: str) -> Decision:
    waiting = await repo.tickets_awaiting_answer(session, requester_id=requester_id)

    if not waiting:
        return Decision(Kind.NEW)

    if len(waiting) > 1:
        # Инвариант D15 нарушен: второй вопрос тому же человеку не даёт задать
        # хендлер очереди. Берём самую старую и сообщаем в лог — молча выбрать
        # произвольную из двух значило бы подшить ответ к чужой проблеме.
        logger.warning(
            "У сотрудника %s %s заявок ждут ответа, ожидалась одна",
            requester_id,
            len(waiting),
        )

    return Decision(Kind.ANSWER, ticket_id=waiting[0].id)
