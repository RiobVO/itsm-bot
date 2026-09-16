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
from aiogram.types import CallbackQuery, Chat, Message, User
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


def _callback(data: str) -> CallbackQuery:
    """Нажатие кнопки под сообщением бота в личке."""
    return CallbackQuery(
        id="cb1",
        from_user=User(id=42, is_bot=False, first_name="Пётр", language_code="ru"),
        chat_instance="ci1",
        data=data,
        message=_message("Заполнить", chat_id=42, chat_type="private"),
    )


async def _first_matching_callback_handler(
    dispatcher, callback: CallbackQuery, raw_state: str | None = None
) -> str | None:
    """То же, что `_first_matching_handler`, но для шины колбэков."""
    for router in dispatcher.sub_routers:
        context: dict[str, object] = {"bot": BOT, "raw_state": raw_state}
        passed, data = await router.callback_query.check_root_filters(
            callback, **context
        )
        if not passed:
            continue

        context.update(data or {})
        for handler in router.callback_query.handlers:
            passed, _data = await handler.check(callback, **context)
            if passed:
                return handler.callback.__name__
    return None


async def _first_matching_handler(
    dispatcher, message: Message, raw_state: str | None = None
) -> str | None:
    """Имя первого хендлера, чьи фильтры пропустили сообщение.

    Повторяет порядок, в котором aiogram обходит роутеры и хендлеры, но ничего не
    выполняет: нас интересует адресат апдейта, а не его обработка.

    Фильтры роутера проверяются отдельно от фильтров хендлера: `handler.check()` про
    первые не знает, и без `check_root_filters` тест решил бы, что вопрос
    исполнителя ловится в личной переписке.
    """
    for router in dispatcher.sub_routers:
        context: dict[str, object] = {"bot": BOT, "raw_state": raw_state}
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


class TestDuringProfileSurvey:
    """Состояния анкеты не должны проглатывать команды.

    Без `raw_state` в контексте этот класс дефектов невидим: фильтр состояния
    пропускает всё, и тест решил бы, что `/my` во время анкеты обрабатывается
    командой, тогда как на живом боте он сохранялся бы как номер кабинета.
    """

    @pytest.mark.parametrize("state", ["Profile:waiting_start", "Profile:room", "Profile:department"])
    async def test_my_is_not_eaten_by_the_survey(self, dispatcher, state: str) -> None:
        message = _message("/my", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message, state) == "my_tickets"

    @pytest.mark.parametrize("state", ["Profile:waiting_start", "Profile:room", "Profile:department"])
    async def test_profile_restarts_the_survey_from_any_state(
        self, dispatcher, state: str
    ) -> None:
        message = _message("/profile", chat_id=42, chat_type="private")

        assert await _first_matching_handler(dispatcher, message, state) == "edit_profile"

    async def test_room_answer_reaches_the_survey(self, dispatcher) -> None:
        message = _message("312", chat_id=42, chat_type="private")

        assert (
            await _first_matching_handler(dispatcher, message, "Profile:room")
            == "receive_room"
        )

    async def test_text_before_the_button_goes_to_the_buffer(self, dispatcher) -> None:
        message = _message("и ещё не печатает", chat_id=42, chat_type="private")

        assert (
            await _first_matching_handler(dispatcher, message, "Profile:waiting_start")
            == "collect_while_waiting"
        )


class TestKeyboardsAfterRestart:
    """Кнопка из сообщения прошлого процесса обязана находить хендлер.

    `MemoryStorage` теряет состояние анкеты при перезапуске, и фильтр по состоянию
    делает кнопку мёртвой: нажатие без хендлера — это молчащий бот. Найдено живым
    прогоном 15.09.2026 на «Заполнить»; кнопка отдела ломалась тем же способом.
    """

    async def test_fill_in_button_works_without_state(self, dispatcher) -> None:
        assert (
            await _first_matching_callback_handler(dispatcher, _callback("profile:start"))
            == "begin_profile"
        )

    async def test_fill_in_button_works_in_its_own_state(self, dispatcher) -> None:
        """Обычный путь не должен пострадать от снятого фильтра."""
        assert (
            await _first_matching_callback_handler(
                dispatcher, _callback("profile:start"), "Profile:waiting_start"
            )
            == "begin_profile"
        )

    async def test_department_button_without_state_is_answered(self, dispatcher) -> None:
        """Номер кабинета пропал вместе с состоянием — анкету придётся начать заново."""
        assert (
            await _first_matching_callback_handler(dispatcher, _callback("dept:0"))
            == "stale_department"
        )

    async def test_department_button_in_its_state_reaches_the_survey(
        self, dispatcher
    ) -> None:
        """Заглушка стоит после рабочего хендлера и не должна перехватывать анкету."""
        assert (
            await _first_matching_callback_handler(
                dispatcher, _callback("dept:0"), "Profile:department"
            )
            == "receive_department"
        )
