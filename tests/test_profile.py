"""Анкета целиком, от точки входа до сохранённого профиля (F1, F2, F3).

Живой прогон 15.09.2026 нашёл мёртвую кнопку «Заполнить»: `MemoryStorage` теряет
состояние анкеты вместе с процессом, а кнопка остаётся в переписке. Здесь
проверяется восстановление — и то, чем анкета заканчивается, когда обращение,
ради которого её показали, унёс перезапуск (D13).

Анкета проходится настоящими хендлерами от настоящей точки входа: `/start`,
обращение, `/profile` или нажатие кнопки без состояния. Подставлять признаки прямо
в данные FSM нельзя — именно так тест и разошёлся бы с боевым путём, где эти
признаки расставляют сами хендлеры. От aiogram хендлерам нужны только `answer` и
`edit_reply_markup`, поэтому объекты Telegram здесь подставные, а сеть не нужна.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot import cards
from itsm_bot.bot.buffer import Batch, MessageBuffer
from itsm_bot.bot.handlers.commands import start
from itsm_bot.bot.handlers.messages import incoming
from itsm_bot.bot.handlers.profile import (
    Profile,
    begin_profile,
    edit_profile,
    receive_department,
    receive_room,
    stale_department,
)
from itsm_bot.config import Settings
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language

TELEGRAM_ID = "42"
DEPARTMENTS = ["Бухгалтерия", "Логистика"]
ROOM = "707"

WINDOW = 0.05
"""Окно склейки в тестах — доли секунды: проверяется поведение, а не 45 секунд N3.

Оно обязано быть коротким: с окном в десяток секунд тест на удержание проходил бы и
на буфере, который ничего не удерживает, — таймер просто не успевал бы сработать.
"""


@dataclass
class _Dialog:
    """Переписка с одним сотрудником: все ответы бота собираются в один список."""

    language_code: str = "ru"
    answers: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    fails: bool = False
    """Отправка не доходит: Telegram отвечает ошибкой чаще, чем хочется помнить."""

    callback_answer_fails: bool = False
    """Подтверждение нажатия падает — обычное дело для просроченного callback query."""

    @property
    def user(self) -> User:
        return User(
            id=int(TELEGRAM_ID),
            is_bot=False,
            first_name="Пётр",
            language_code=self.language_code,
        )

    def message(self, text: str = "") -> Any:
        return _FakeMessage(dialog=self, text=text)

    def callback(self, data: str) -> Any:
        return _FakeCallback(dialog=self, data=data)


@dataclass
class _FakeChat:
    id: int = 42


@dataclass
class _FakeMessage:
    dialog: _Dialog
    text: str = ""
    message_id: int = 100
    chat: _FakeChat = field(default_factory=_FakeChat)

    @property
    def from_user(self) -> User:
        return self.dialog.user

    async def answer(self, text: str, **kwargs: Any) -> None:
        if self.dialog.fails:
            raise RuntimeError("Telegram недоступен")
        self.dialog.answers.append(text)

    async def edit_reply_markup(self, **kwargs: Any) -> None:
        pass


@dataclass
class _FakeCallback:
    dialog: _Dialog
    data: str

    @property
    def from_user(self) -> User:
        return self.dialog.user

    @property
    def message(self) -> _FakeMessage:
        return _FakeMessage(dialog=self.dialog)

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        if self.dialog.callback_answer_fails:
            raise RuntimeError("query is too old")
        if text is not None:
            self.dialog.alerts.append(text)


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=42, user_id=int(TELEGRAM_ID)),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="123:abc",  # type: ignore[arg-type]
        queue_chat_id=-1001234567890,
        departments=DEPARTMENTS,
    )


@pytest.fixture
def buffer() -> tuple[MessageBuffer, list[Batch]]:
    collected: list[Batch] = []

    async def flush(batch: Batch) -> None:
        collected.append(batch)

    return MessageBuffer(window_seconds=WINDOW, flush=flush), collected


async def _answer_survey(
    dialog: _Dialog,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    message_buffer: MessageBuffer,
) -> None:
    """Ответы на оба вопроса анкеты — с того места, где задан вопрос про кабинет."""
    await receive_room(dialog.message(ROOM), state, settings)
    await receive_department(
        dialog.callback("dept:1"), state, session, settings, message_buffer
    )


def _repeat_request(language: Language = Language.RU) -> str:
    return cards.say("repeat_request", language)


class TestSurveyAfterRestart:
    async def test_dead_button_comes_back_to_life(
        self, session: AsyncSession, buffer
    ) -> None:
        """Нажатие кнопки из прошлого процесса обязано задать вопрос про кабинет."""
        message_buffer, _collected = buffer
        dialog = _Dialog()
        state = _state()

        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )

        assert dialog.answers == [cards.say("ask_room", Language.RU)]
        assert await state.get_state() == Profile.room.state

    async def test_survey_survives_to_the_next_step(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Восстановленный язык обязан лечь в данные анкеты.

        Без этого следующий шаг падает на `data["language"]`, и анкета обрывается
        там, где человек уже ответил.
        """
        message_buffer, _collected = buffer
        dialog = _Dialog()
        state = _state()

        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await _answer_survey(dialog, state, session, settings, message_buffer)

        employee = await repo.get_employee(session, TELEGRAM_ID)
        assert employee is not None
        assert (employee.room, employee.department) == (ROOM, "Логистика")

    async def test_buffer_is_held_again(self, session: AsyncSession, buffer) -> None:
        """Удержание пропало вместе с состоянием.

        Без него окно склейки снимет обращение, написанное после кнопки, раньше,
        чем появится профиль, — это ровно тот отказ, ради которого есть `hold`.
        """
        message_buffer, collected = buffer
        dialog = _Dialog()

        await begin_profile(
            dialog.callback("profile:start"), _state(), session, message_buffer
        )
        await message_buffer.add(
            TELEGRAM_ID, text="принтер", chat_id=42, message_id=100
        )
        await asyncio.sleep(WINDOW * 4)

        assert collected == []

    async def test_failed_question_does_not_leave_the_buffer_held(
        self, session: AsyncSession, buffer
    ) -> None:
        """Удержание ставится только после доставленного вопроса.

        Иначе упавшая отправка оставляет буфер held без состояния анкеты, и для
        сотрудника с профилем бот молчит до перезапуска: его сообщения копятся, но
        таймер снят, а приглашения заново он не получит.
        """
        message_buffer, collected = buffer
        dialog = _Dialog(fails=True)

        with pytest.raises(RuntimeError):
            await begin_profile(
                dialog.callback("profile:start"), _state(), session, message_buffer
            )

        await message_buffer.add(
            TELEGRAM_ID, text="принтер", chat_id=42, message_id=100
        )
        await asyncio.sleep(WINDOW * 4)

        assert [item.text for item in collected] == ["принтер"]


class TestSurveyLanguage:
    async def test_falls_back_to_the_telegram_locale(
        self, session: AsyncSession, buffer
    ) -> None:
        """Профиля нет — язык брать неоткуда, кроме локали Telegram."""
        message_buffer, _collected = buffer
        dialog = _Dialog(language_code="en")

        await begin_profile(
            dialog.callback("profile:start"), _state(), session, message_buffer
        )

        assert dialog.answers == [cards.say("ask_room", Language.EN)]

    async def test_saved_language_wins_over_the_locale(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Иначе анкета по старой кнопке молча переключила бы профиль на другой язык.

        `receive_department` записывает выбранный здесь язык обратно в `employees`,
        и локаль клиента затёрла бы выбор сотрудника (D14).
        """
        message_buffer, _collected = buffer
        await repo.save_employee(
            session,
            telegram_id=TELEGRAM_ID,
            display_name="Пётр",
            room="311",
            department=DEPARTMENTS[0],
            language=Language.EN,
        )
        dialog = _Dialog(language_code="ru")
        state = _state()

        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert dialog.answers[0] == cards.say("ask_room", Language.EN)
        employee = await repo.get_employee(session, TELEGRAM_ID)
        assert employee is not None
        assert employee.language is Language.EN

    async def test_stale_department_answers_in_the_saved_language(
        self, session: AsyncSession
    ) -> None:
        await repo.save_employee(
            session,
            telegram_id=TELEGRAM_ID,
            display_name="Пётр",
            room="311",
            department=DEPARTMENTS[0],
            language=Language.EN,
        )
        dialog = _Dialog(language_code="ru")

        await stale_department(dialog.callback("dept:1"), session)

        assert dialog.alerts == [cards.say("profile_interrupted", Language.EN)]


class TestStaleDepartmentButton:
    async def test_press_without_state_is_answered(
        self, session: AsyncSession
    ) -> None:
        """Молчание на нажатие читается как сломанный бот — ради этого и заглушка."""
        dialog = _Dialog()

        await stale_department(dialog.callback("dept:1"), session)

        assert dialog.alerts == [cards.say("profile_interrupted", Language.RU)]


class TestWhenTheSurveyEndsWithoutATicket:
    """Чем кончается анкета, если заявки из неё не вышло.

    Разбирается по точке входа, потому что только она знает, ждало ли профиля
    обращение. Просьба повторить обращение тому, кто ничего не писал, — ложь, а
    молчание тому, чьё обращение потерялось, — это и есть исходная находка прогона.
    """

    async def test_request_lost_to_a_restart_is_asked_for_again(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Кнопка из прошлого процесса: обращение было, но буфер его не пережил."""
        message_buffer, collected = buffer
        dialog = _Dialog()
        state = _state()

        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert collected == []
        assert dialog.answers[-1] == _repeat_request()

    async def test_survey_resumed_through_the_profile_command_too(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Перезапуск на выборе отдела уводит в `/profile` — обращение так же потеряно.

        Сюда человека отправляет `stale_department`, и без признака у этой точки
        входа анкета кончалась бы сохранённым профилем и тишиной вместо заявки.
        """
        message_buffer, collected = buffer
        dialog = _Dialog()
        state = _state()

        await edit_profile(dialog.message("/profile"), state, session, message_buffer)
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert collected == []
        assert dialog.answers[-1] == _repeat_request()

    async def test_start_command_does_not_invent_a_lost_request(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """`/start` — знакомство: буфер пуст законно, и просить нечего повторять."""
        message_buffer, _collected = buffer
        dialog = _Dialog()
        state = _state()

        await start(dialog.message("/start"), state, session, message_buffer)
        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert _repeat_request() not in dialog.answers

    async def test_profile_edit_says_nothing_extra(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """У сотрудника с профилем правка кабинета обращения и не подразумевает."""
        message_buffer, _collected = buffer
        await repo.save_employee(
            session,
            telegram_id=TELEGRAM_ID,
            display_name="Пётр",
            room="311",
            department=DEPARTMENTS[0],
            language=Language.RU,
        )
        dialog = _Dialog()
        state = _state()

        await edit_profile(dialog.message("/profile"), state, session, message_buffer)
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert _repeat_request() not in dialog.answers

    async def test_failed_callback_acknowledgement_does_not_swallow_the_request(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Просроченный callback query не должен уносить с собой просьбу.

        Подтверждение нажатия гасит часики у кнопки и падает штатно. Если оно стоит
        раньше просьбы повторить обращение, человек видит сохранённый профиль — и
        ни номера заявки, ни объяснения.
        """
        message_buffer, _collected = buffer
        dialog = _Dialog()
        state = _state()

        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await receive_room(dialog.message(ROOM), state, settings)

        dialog.callback_answer_fails = True
        with pytest.raises(RuntimeError):
            await receive_department(
                dialog.callback("dept:1"), state, session, settings, message_buffer
            )

        assert dialog.answers[-1] == _repeat_request()

    async def test_request_that_reached_the_queue_is_not_asked_for_again(
        self, session: AsyncSession, settings: Settings, buffer
    ) -> None:
        """Обычный путь: обращение дождалось профиля и ушло в заявку."""
        message_buffer, collected = buffer
        dialog = _Dialog()
        state = _state()

        await incoming(
            dialog.message("принтер не печатает"), state, session, message_buffer
        )
        await begin_profile(
            dialog.callback("profile:start"), state, session, message_buffer
        )
        await _answer_survey(dialog, state, session, settings, message_buffer)

        assert [item.text for item in collected] == ["принтер не печатает"]
        assert _repeat_request() not in dialog.answers
