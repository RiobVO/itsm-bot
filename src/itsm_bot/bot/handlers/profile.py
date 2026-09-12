"""Анкета при первом обращении (F1, F2) и её правка (F3).

Первое сообщение незнакомого человека не теряется: оно ждёт в удержанном буфере и
превращается в заявку сразу после сохранения профиля. Спрашивать «повторите
обращение» после анкеты значило бы заставить человека написать одно и то же дважды.

Анкета начинается с кнопки, а не сразу с вопроса. Человек пишет очередями (D7):
«не работает VPN» → «пароль qwerty123». Если первое сообщение сразу переводит FSM в
состояние «жду кабинет», второе окажется ответом на вопрос, которого человек не
видел, и ляжет в профиль. Пока кнопка не нажата, весь текст остаётся обращением.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot import cards
from itsm_bot.bot.buffer import MessageBuffer
from itsm_bot.config import Settings
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language

logger = logging.getLogger(__name__)

router = Router(name="profile")
router.message.filter(F.chat.type == "private")


class Profile(StatesGroup):
    waiting_start = State()
    room = State()
    department = State()


async def invite_to_profile(
    message: Message, state: FSMContext, language: Language, buffer: MessageBuffer
) -> None:
    """Предлагает заполнить профиль и останавливает окно склейки.

    Вопрос про кабинет здесь не задаётся: пока человек не нажал кнопку, его текст —
    это продолжение обращения, а не ответ анкеты.
    """
    buffer.hold(str(message.from_user.id))
    await state.set_state(Profile.waiting_start)
    await state.update_data(language=language.value)
    await message.answer(
        cards.say("profile_needed", language),
        reply_markup=cards.start_profile_keyboard(language),
    )


@router.message(Profile.waiting_start, F.text, ~F.text.startswith("/"))
async def collect_while_waiting(
    message: Message, state: FSMContext, buffer: MessageBuffer
) -> None:
    """Текст до нажатия кнопки — продолжение обращения, копится в удержанном буфере.

    Команды сюда не попадают: `~F.text.startswith("/")` пропускает их дальше, иначе
    человек, не нажавший кнопку, не смог бы выбраться отсюда ни `/start`, ни
    `/profile` — они молча становились бы строками обращения.

    Приглашение повторяется ровно один раз. Без него человек, продолжающий писать,
    не получает ни ответа, ни объяснения; повторять на каждое сообщение — спам при
    обычной очереди из трёх фраз.
    """
    await buffer.add(
        str(message.from_user.id),
        text=message.text,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )

    data = await state.get_data()
    if data.get("reminded"):
        return

    language = Language(data["language"])
    await state.update_data(reminded=True)
    await message.answer(
        cards.say("profile_needed", language),
        reply_markup=cards.start_profile_keyboard(language),
    )


@router.callback_query(Profile.waiting_start, F.data == "profile:start")
async def begin_profile(callback: CallbackQuery, state: FSMContext) -> None:
    """Состояние переключается только после того, как вопрос доставлен.

    В обратном порядке упавшая отправка оставила бы FSM в `Profile.room` при
    невидимом вопросе, и следующая фраза обращения стала бы номером кабинета.
    """
    data = await state.get_data()
    language = Language(data["language"])

    await callback.message.answer(cards.say("ask_room", language))
    await state.set_state(Profile.room)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.message(Command("profile"))
async def edit_profile(
    message: Message, state: FSMContext, session: AsyncSession, buffer: MessageBuffer
) -> None:
    """Правка профиля по явной команде — кнопка здесь не нужна, намерение уже названо."""
    employee = await repo.get_employee(session, str(message.from_user.id))
    language = (
        employee.language
        if employee
        else cards.language_from_code(message.from_user.language_code)
    )
    buffer.hold(str(message.from_user.id))
    await state.set_state(Profile.room)
    await state.update_data(language=language.value)
    await message.answer(cards.say("profile_restart", language))


@router.message(Profile.room, F.text, ~F.text.startswith("/"))
async def receive_room(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    language = Language(data["language"])
    room = message.text.strip()

    if len(room) > cards.ROOM_MAX_LENGTH or "\n" in room:
        # Вставленный кусок обращения не должен осесть в профиле.
        await message.answer(cards.say("room_too_long", language))
        return

    # Вопрос отправляется до смены состояния: в обратном порядке упавшая отправка
    # оставила бы FSM ждать нажатия по клавиатуре, которой человек не получил.
    await message.answer(
        cards.say("ask_department", language),
        reply_markup=cards.departments_keyboard(settings.departments),
    )
    await state.update_data(room=room)
    await state.set_state(Profile.department)


@router.callback_query(Profile.department, F.data.startswith("dept:"))
async def receive_department(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    buffer: MessageBuffer,
) -> None:
    index = int(callback.data.removeprefix("dept:"))
    telegram_id = str(callback.from_user.id)

    if not 0 <= index < len(settings.departments):
        # Справочник правили, пока человек держал открытой старую клавиатуру.
        await callback.answer(
            "Список отделов изменился, наберите /profile", show_alert=True
        )
        await state.clear()
        await buffer.release(telegram_id)
        return

    data = await state.get_data()
    language = Language(data["language"])
    department = settings.departments[index]

    await repo.save_employee(
        session,
        telegram_id=telegram_id,
        display_name=callback.from_user.full_name,
        room=data["room"],
        department=department,
        language=language,
    )

    try:
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            cards.say(
                "profile_saved", language, room=data["room"], department=department
            )
        )
        await callback.answer()
    finally:
        # Снятие удержания не должно зависеть от того, дошли ли поздравления:
        # иначе упавший вызов Telegram оставит буфер held навсегда, и бот замолчит
        # для этого человека до перезапуска.
        await buffer.release(telegram_id)


@router.message(Profile.room, ~F.text.startswith("/"))
@router.message(Profile.department, ~F.text.startswith("/"))
async def ignore_non_text_during_profile(message: Message) -> None:
    """Стикер вместо номера кабинета не должен ломать анкету и не должен молчать.

    Команды исключены: иначе `/my` во время анкеты сохранился бы как номер кабинета,
    а из состояния выбора отдела нельзя было бы выйти вообще ничем.
    """
    await message.answer("Ответьте, пожалуйста, текстом.")
