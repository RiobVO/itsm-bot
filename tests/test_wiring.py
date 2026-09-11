"""Тесты проводки: какой хендлер получит апдейт.

Порядок роутеров и их фильтры — место, где независимое ревью дважды нашло дефект:
хендлер, «просто вернувший управление», в aiogram считается сработавшим и обрывает
распространение апдейта. Из-за этого команда в личке молча пропадала, а команда,
отправленная реплаем в группе, уходила заявителю как вопрос.

Проверяются фильтры, а не выполнение: сетевой доступ для этого не нужен.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Chat, Message, User
from sqlalchemy.ext.asyncio import async_sessionmaker

from itsm_bot.bot.main import build_dispatcher
from itsm_bot.config import Settings

QUEUE_CHAT_ID = -1001234567890


BOT = Bot(
    "123:abcdefghijklmnopqrstuvwxyz",
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    """Диспетчер собирается один раз на модуль.

    Роутеры — модульные объекты, и второй `Dispatcher` их уже не примет: aiogram
    запрещает присоединять роутер дважды. В боевом процессе диспетчер и так один.

    Фильтры до БД не доходят, поэтому фабрика сессий здесь нерабочая — она нужна
    только чтобы собрать зависимости.
    """
    settings = Settings(
        bot_token="123:abc",  # type: ignore[arg-type]
        queue_chat_id=QUEUE_CHAT_ID,
        departments=["Бухгалтерия", "Продажи"],
    )
    built, _buffer, _warn = build_dispatcher(
        settings=settings,
        session_factory=async_sessionmaker(),  # type: ignore[call-arg]
        bot=BOT,
    )
    return built


def _message(
    text: str,
    *,
    chat_id: int,
    chat_type: str,
    reply_to: Message | None = None,
) -> Message:
    return Message(
        message_id=10,
        date=datetime(2026, 9, 14, tzinfo=UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=42, is_bot=False, first_name="Пётр"),
        text=text,
        reply_to_message=reply_to,
    )


async def _first_matching_handler(dispatcher, message: Message) -> str | None:
    """Имя первого хендлера, чьи фильтры пропустили сообщение.

    Повторяет порядок, в котором aiogram обходит роутеры и хендлеры, но ничего не
    выполняет: нас интересует адресат апдейта, а не его обработка.

    Фильтры роутера проверяются отдельно от фильтров хендлера: `handler.check()` про
    первые не знает, и без `check_root_filters` тест решил бы, что вопрос
    исполнителя ловится в личной переписке.
    """
    for router in dispatcher.sub_routers:
        context: dict[str, object] = {"bot": BOT}
        passed, data = await router.message.check_root_filters(message, **context)
        if not passed:
            continue

        context.update(data or {})
        for handler in router.message.handlers:
            passed, _data = await handler.check(message, **context)
            if passed:
                return handler.callback.__name__
    return None


class TestCommandsInPrivateChat:
    async def test_stats_does_not_swallow_the_update(self, dispatcher) -> None:
        """Команда исполнителя в личке обязана дойти до подсказки, а не пропасть молча."""
        message = _message("/stats", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "unknown_command"

    @pytest.mark.parametrize("command", ["/queue", "/ticket 47", "/publish 47"])
    async def test_executor_commands_fall_through(self, dispatcher, command: str) -> None:
        message = _message(command, chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "unknown_command"

    async def test_start_is_handled_and_never_becomes_a_ticket(self, dispatcher) -> None:
        message = _message("/start", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "start"

    async def test_my_is_handled(self, dispatcher) -> None:
        message = _message("/my", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "my_tickets"

    async def test_profile_is_handled(self, dispatcher) -> None:
        message = _message("/profile", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "edit_profile"

    async def test_plain_text_goes_to_the_buffer(self, dispatcher) -> None:
        message = _message("принтер не печатает", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message) == "incoming"


class TestPrivateReply:
    async def test_reply_in_private_is_not_taken_by_the_queue_router(
        self, dispatcher
    ) -> None:
        """Ответ заявителя реплаем на сообщение бота обязан попасть в буфер."""
        original = _message("Вопрос по заявке #47", chat_id=42, chat_type="private")
        message = _message("Третий этаж", chat_id=42, chat_type="private", reply_to=original)

        assert await _first_matching_handler(dispatcher, message) == "incoming"


class TestGroupChat:
    async def test_reply_on_a_card_is_a_question(self, dispatcher) -> None:
        card = _message("🆕 Заявка #47", chat_id=QUEUE_CHAT_ID, chat_type="supergroup")
        message = _message(
            "какая модель?", chat_id=QUEUE_CHAT_ID, chat_type="supergroup", reply_to=card
        )

        assert await _first_matching_handler(dispatcher, message) == "ask_requester"

    async def test_command_sent_as_reply_is_not_a_question(self, dispatcher) -> None:
        """Иначе `/queue` реплаем на карточку ушёл бы заявителю и завёл ожидание ответа."""
        card = _message("🆕 Заявка #47", chat_id=QUEUE_CHAT_ID, chat_type="supergroup")
        message = _message(
            "/queue", chat_id=QUEUE_CHAT_ID, chat_type="supergroup", reply_to=card
        )

        assert await _first_matching_handler(dispatcher, message) == "show_queue"

    async def test_ordinary_group_message_is_ignored(self, dispatcher) -> None:
        """Обычная реплика в группе — не обращение и не вопрос."""
        message = _message("ага", chat_id=QUEUE_CHAT_ID, chat_type="supergroup")

        assert await _first_matching_handler(dispatcher, message) is None


class TestRouterOrder:
    async def test_messages_router_is_last(self, dispatcher) -> None:
        """`messages` ловит любой текст, поэтому обязан идти после всех остальных."""
        names = [router.name for router in dispatcher.sub_routers]

        assert names == ["profile", "queue", "commands", "messages"]
