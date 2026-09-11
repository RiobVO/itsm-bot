"""Сборка и запуск бота."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_bot.bot import cards
from itsm_bot.bot.buffer import Batch, MessageBuffer
from itsm_bot.bot.handlers import commands, messages, profile, queue
from itsm_bot.bot.intake import Intake, Outcome
from itsm_bot.bot.middlewares import ServicesMiddleware
from itsm_bot.config import Settings, get_settings
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language
from itsm_bot.storage.session import create_session_factory

logger = logging.getLogger(__name__)


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> tuple[Dispatcher, MessageBuffer, Callable[[], Awaitable[None]]]:
    """Собирает приложение без запуска polling.

    Вынесено из `run()`, чтобы сборку можно было проверить тестом: порядок роутеров
    и их фильтры уже дважды оказывались местом, где апдейт съедался не тем
    хендлером, а сетевого доступа для этой проверки не нужно.

    Возвращает диспетчер, буфер (его надо закрыть при остановке) и функцию
    предупреждения о заявках без карточки.
    """

    async def language_of(telegram_id: str) -> Language:
        async with session_factory() as session:
            employee = await repo.get_employee(session, telegram_id)
        return employee.language if employee else Language.RU

    async def publish(ticket_id: int) -> int | None:
        """Карточка в группу очереди.

        Ошибка отправки не отменяет заявку: она уже в БД, и потерять её хуже, чем
        остаться без карточки. Заявка без карточки видна в /queue и досылается
        вручную командой /publish — автоматическая досылка создавала бы дубли.
        """
        async with session_factory() as session:
            ticket = await repo.get_ticket(session, ticket_id)
            if ticket is None:
                return None
            employee = await repo.get_employee(session, ticket.requester_id)
            text = cards.card_text(
                ticket,
                display_name=employee.display_name if employee else None,
                limit=settings.card_text_limit,
            )
            markup = cards.status_keyboard(ticket.id, ticket.status)

        try:
            sent = await bot.send_message(
                settings.queue_chat_id, text, reply_markup=markup
            )
        except Exception:
            logger.exception("Не удалось отправить карточку заявки #%s", ticket_id)
            return None
        return sent.message_id

    async def warn_about_missing_cards() -> None:
        """Сообщает исполнителю, что какие-то заявки остались без карточки.

        Именно сообщает, а не досылает: успешная отправка и запись `queue_message_id`
        разделены, и в окне между ними автоматическая досылка удвоила бы карточку.
        """
        async with session_factory() as session:
            missing = [
                ticket.id
                for ticket in await repo.queue(session)
                if ticket.queue_message_id is None
            ]
        if not missing:
            return

        numbers = ", ".join(f"#{number}" for number in missing)
        try:
            await bot.send_message(
                settings.queue_chat_id,
                f"Без карточки в очереди: {numbers}. Досылка — /publish <номер>.",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Не удалось предупредить о заявках без карточки")

    async def flush(batch: Batch) -> None:
        """Пачка собралась — обрабатываем и отвечаем сотруднику на его языке.

        Пачка снимается с буфера до вызова, поэтому упавшая обработка означает
        потерянное обращение. Человек должен об этом узнать: молчание он примет за
        принятую заявку и будет ждать ответа.
        """
        language = await language_of(batch.telegram_id)
        try:
            result = await intake.process(batch)
        except Exception:
            logger.exception(
                "Не удалось обработать пачку сотрудника %s", batch.telegram_id
            )
            await bot.send_message(
                batch.chat_id, cards.say("processing_failed", language)
            )
            return

        if result.outcome is Outcome.CREATED and result.ticket_id is not None:
            await bot.send_message(
                batch.chat_id,
                cards.say("ticket_created", language, ticket_id=result.ticket_id),
            )
            if result.security_flag:
                await bot.send_message(
                    batch.chat_id, cards.say("secret_warning", language)
                )
        elif result.outcome is Outcome.ANSWERED and result.ticket_id is not None:
            await bot.send_message(
                batch.chat_id,
                cards.say("answer_saved", language, ticket_id=result.ticket_id),
            )
        elif result.outcome is Outcome.DUPLICATE and result.ticket_id is not None:
            await bot.send_message(
                batch.chat_id,
                cards.say("ticket_duplicate", language, ticket_id=result.ticket_id),
            )
        elif result.outcome is Outcome.RATE_LIMITED:
            await bot.send_message(
                batch.chat_id, cards.say("ticket_rate_limited", language)
            )

    buffer = MessageBuffer(
        window_seconds=settings.buffer_seconds, flush=lambda item: flush(item)
    )
    intake = Intake(
        session_factory=session_factory,
        buffer=buffer,
        publish=publish,
        rate_limit_per_hour=settings.rate_limit_per_hour,
        dedup_window_minutes=settings.dedup_window_minutes,
    )

    # Изоляция событий: без неё два нажатия по одной заявке идут параллельно, и
    # карточка расходится с БД.
    dispatcher = Dispatcher(
        storage=MemoryStorage(), events_isolation=SimpleEventIsolation()
    )
    dispatcher.update.middleware(
        ServicesMiddleware(
            session_factory=session_factory,
            intake=intake,
            buffer=buffer,
            settings=settings,
            publish=publish,
        )
    )
    # Порядок важен: `messages` ловит любой текст и обязан идти последним.
    dispatcher.include_router(profile.router)
    dispatcher.include_router(queue.router)
    dispatcher.include_router(commands.router)
    dispatcher.include_router(messages.router)

    return dispatcher, buffer, warn_about_missing_cards


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    session_factory = create_session_factory(settings.database_url)
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher, buffer, warn_about_missing_cards = build_dispatcher(
        settings=settings, session_factory=session_factory, bot=bot
    )

    await warn_about_missing_cards()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await buffer.close()
        await bot.session.close()
