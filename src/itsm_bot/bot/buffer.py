"""Буфер склейки сообщений и блокировки на сотрудника.

В личку пишут очередями (D7), поэтому сообщения одного человека копятся здесь и
уходят в заявку через `window_seconds` после последнего.

Буфер держит **сырой** текст, поэтому живёт только в памяти (D13): на диск он лечь
не может по N8, а маскировать до склейки нельзя — `redact()` работает по контексту,
и находка, разорванная границей сообщений, потеряется.

Здесь же реестр блокировок. Создание заявки обязано быть сериализовано на
`telegram_id`, иначе две параллельные обработки обойдут и проверку дубля, и
рейт-лимит. Блокировка на сотруднике, а не глобальная: обращения разных людей
обрабатываются параллельно, гонка возможна только внутри одного.

Три правила, каждое найдено независимым ревью на конкретном сценарии отказа:

1. Блокировка не удерживается во время обработки пачки. Обработчик берёт ту же
   блокировку сам — это и есть точка сериализации, а `asyncio.Lock` не реентрантен.
2. Таймер не отменяет сам себя: `CancelledError` не наследуется от `Exception`,
   и самоотмена погасила бы обработку без следа в логах.
3. Анкета приостанавливает окно: заполнение кабинета и отдела занимает больше
   45 секунд, и без удержания первое обращение уходит в обработку без профиля.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from itsm_bot.storage.models import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Batch:
    """Склеенная пачка сообщений — заготовка одной заявки."""

    telegram_id: str
    text: str
    chat_id: int
    first_message_id: int

    started_at: datetime
    """Когда пришло первое сообщение пачки.

    Нужно, чтобы отличить ответ на вопрос от обращения, написанного раньше вопроса:
    человек пишет в 10:00, исполнитель задаёт вопрос в 10:00:10, пачка собирается в
    10:00:45 — без этой отметки она подшилась бы ответом на вопрос, которого автор
    ещё не видел.
    """


@dataclass
class _Pending:
    lines: list[str] = field(default_factory=list)
    chat_id: int = 0
    first_message_id: int = 0
    started_at: datetime = field(default_factory=utcnow)
    timer: asyncio.Task[None] | None = None


class MessageBuffer:
    def __init__(
        self, *, window_seconds: float, flush: Callable[[Batch], Awaitable[None]]
    ) -> None:
        self._window = window_seconds
        self._flush = flush
        self._pending: dict[str, _Pending] = {}
        self._held: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, telegram_id: str) -> asyncio.Lock:
        """Блокировка сотрудника. Реестр не чистится: при N1 = 50–200 он ограничен."""
        return self._locks.setdefault(telegram_id, asyncio.Lock())

    async def add(
        self, telegram_id: str, *, text: str, chat_id: int, message_id: int
    ) -> None:
        """Добавляет сообщение в пачку и перезапускает окно."""
        # Время события фиксируется ДО ожидания блокировки. Внутри неё оно было бы
        # временем не получения сообщения, а получения доступа: обработка предыдущей
        # пачки держит ту же блокировку на сетевой публикации, и сообщение,
        # пришедшее раньше вопроса исполнителя, получило бы отметку позже него — и
        # снова подшилось бы ответом на невидимый вопрос.
        #
        # Источник времени — собственные часы, а не `message.date`: отметки вопросов
        # ставит та же `utcnow()`, и сравнивать их со временем Telegram значило бы
        # сравнивать показания двух часов с неизвестным расхождением.
        received_at = utcnow()

        async with self.lock(telegram_id):
            pending = self._pending.get(telegram_id)
            if pending is None:
                pending = _Pending(
                    chat_id=chat_id,
                    first_message_id=message_id,
                    started_at=received_at,
                )
                self._pending[telegram_id] = pending

            pending.lines.append(text)
            self._cancel_timer(pending)
            if telegram_id not in self._held:
                pending.timer = asyncio.create_task(self._wait_and_flush(telegram_id))

    def hold(self, telegram_id: str) -> None:
        """Останавливает окно на время анкеты.

        Заполнение кабинета и отдела занимает больше 45 секунд у любого живого
        человека. Без удержания таймер снял бы пачку раньше, чем появился профиль,
        и первое обращение исчезло бы — это F1.
        """
        self._held.add(telegram_id)
        pending = self._pending.get(telegram_id)
        if pending is not None:
            self._cancel_timer(pending)

    async def release(self, telegram_id: str) -> bool:
        """Снимает удержание и отправляет пачку немедленно: ждать больше нечего.

        Возвращает, ушла ли пачка. Анкету можно закончить и в процессе, который
        обращения не получал: перезапуск уносит буфер (D13), а кнопка остаётся в
        переписке. Тогда заявки не будет, и сказать об этом человеку должен
        вызывающий — молча закончить анкету значит оставить его ждать номер.
        """
        self._held.discard(telegram_id)
        batch = await self._take(telegram_id)
        if batch is None:
            return False
        await self._deliver(batch)
        return True

    async def close(self) -> None:
        """Снимает таймеры при остановке процесса. Недособранное не досылается (D13)."""
        for pending in self._pending.values():
            self._cancel_timer(pending)
        self._pending.clear()
        self._held.clear()

    @staticmethod
    def _cancel_timer(pending: _Pending) -> None:
        """Отменяет таймер, кроме случая, когда он и есть текущая задача.

        Самоотмена привела бы к `CancelledError` на ближайшем `await` внутри
        обработки пачки. `CancelledError` не наследуется от `Exception`, обычным
        `except` не ловится, и обращение пропало бы без следа в логах.
        """
        timer = pending.timer
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        pending.timer = None

    async def _wait_and_flush(self, telegram_id: str) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return

        batch = await self._take(telegram_id)
        if batch is not None:
            await self._deliver(batch)

    async def _take(self, telegram_id: str) -> Batch | None:
        """Снимает пачку с буфера под блокировкой и отдаёт её наружу.

        Блокировка держится только здесь и не охватывает обработку: обработчик берёт
        ту же блокировку сам — в этом и состоит сериализация создания заявки. Держать
        её во время вызова значило бы ждать самого себя, `asyncio.Lock` не реентрантен.
        """
        async with self.lock(telegram_id):
            pending = self._pending.pop(telegram_id, None)
            if pending is None:
                return None
            self._cancel_timer(pending)
            return Batch(
                telegram_id=telegram_id,
                text="\n".join(pending.lines),
                chat_id=pending.chat_id,
                first_message_id=pending.first_message_id,
                started_at=pending.started_at,
            )

    async def _deliver(self, batch: Batch) -> None:
        try:
            await self._flush(batch)
        except Exception:
            logger.exception(
                "Не удалось обработать пачку сотрудника %s", batch.telegram_id
            )
