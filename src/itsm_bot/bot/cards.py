"""Тексты, карточка очереди и клавиатуры.

Интерфейса как такового нет — всё рисует клиент Telegram, а управляем мы только
текстом, разметкой HTML и раскладкой кнопок. Поэтому представление собрано в одном
модуле: он не ходит в БД и не требует работающего бота, `InlineKeyboardMarkup` —
обычный объект данных.

Ответы сотруднику — на его языке (D14). Ключи текстов английские, значения русские
и английские.
"""

from __future__ import annotations

from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from itsm_bot.storage import repo
from itsm_bot.storage.models import Language, Ticket, TicketStatus

TELEGRAM_MESSAGE_LIMIT = 4096
ROOM_MAX_LENGTH = 32

STATUS_EMOJI = {
    TicketStatus.NEW: "🆕",
    TicketStatus.IN_PROGRESS: "🔧",
    TicketStatus.DONE: "✅",
    TicketStatus.CANCELLED: "✖️",
}

TEXTS: dict[Language, dict[str, str]] = {
    Language.RU: {
        "profile_needed": "Принял. Чтобы завести заявку, нужны ваш кабинет и отдел — спрошу один раз.",
        "profile_start_button": "Заполнить",
        "ask_room": "В каком кабинете вы сидите?",
        "ask_department": "Ваш отдел?",
        "room_too_long": "Похоже, это не номер кабинета. Напишите только кабинет — например, 312.",
        "profile_saved": "Готово. Кабинет {room}, отдел {department}.",
        "profile_restart": "Обновим профиль. В каком кабинете вы сидите?",
        "profile_interrupted": "Анкета прервалась — наберите /profile, чтобы заполнить её заново. Если вы писали обращение и не получили номер заявки, отправьте его ещё раз.",
        "repeat_request": "Если вы писали обращение и не получили номер заявки — напишите, пожалуйста, ещё раз.",
        "ticket_created": "Заявка #{ticket_id} создана. Напишу, когда возьму в работу.",
        "ticket_duplicate": "Такое обращение уже заведено — заявка #{ticket_id}.",
        "ticket_rate_limited": "Слишком много заявок за час. Если это срочно — напишите исполнителю напрямую.",
        "ticket_new": "🆕 Заявка #{ticket_id} создана.",
        "ticket_in_progress": "🔧 Заявка #{ticket_id} — взял в работу.",
        "ticket_done": "✅ Заявка #{ticket_id} закрыта. Если проблема осталась — просто напишите сюда.",
        "ticket_cancelled": "✖️ Заявка #{ticket_id} отменена.",
        "secret_warning": "⚠️ В сообщении был фрагмент, похожий на пароль — он заменён и нигде не сохранён. Пожалуйста, не присылайте пароли в переписке.",
        "question": "Вопрос по заявке #{ticket_id}:\n{text}",
        "answer_saved": "Ответ записан в заявку #{ticket_id}.",
        "processing_failed": "Не смог обработать сообщение. Пожалуйста, напишите ещё раз.",
        "unknown_command": "Не знаю такой команды. Просто опишите проблему словами — этого достаточно.",
        "start_known": "Опишите проблему своими словами — заведу заявку. /my — ваши заявки, /profile — сменить кабинет.",
        "my_empty": "Открытых заявок нет.",
        "my_header": "Ваши открытые заявки:",
    },
    Language.EN: {
        "profile_needed": "Got it. To file a ticket I need your room and department — I'll ask once.",
        "profile_start_button": "Fill in",
        "ask_room": "Which room are you in?",
        "ask_department": "Your department?",
        "room_too_long": "That doesn't look like a room number. Send just the room — for example, 312.",
        "profile_saved": "Done. Room {room}, department {department}.",
        "profile_restart": "Let's update your profile. Which room are you in?",
        "profile_interrupted": "The survey was interrupted — send /profile to fill it in again. If you sent a request and got no ticket number, send it again.",
        "repeat_request": "If you sent a request and got no ticket number, please send it again.",
        "ticket_created": "Ticket #{ticket_id} created. I'll write when I pick it up.",
        "ticket_duplicate": "This request is already filed as ticket #{ticket_id}.",
        "ticket_rate_limited": "Too many tickets this hour. If it's urgent, message the technician directly.",
        "ticket_new": "🆕 Ticket #{ticket_id} created.",
        "ticket_in_progress": "🔧 Ticket #{ticket_id} — picked up.",
        "ticket_done": "✅ Ticket #{ticket_id} is closed. If the problem is still there, just write here.",
        "ticket_cancelled": "✖️ Ticket #{ticket_id} cancelled.",
        "secret_warning": "⚠️ Your message contained something that looked like a password — it was replaced and stored nowhere. Please don't send passwords in chat.",
        "question": "Question about ticket #{ticket_id}:\n{text}",
        "answer_saved": "Your answer was added to ticket #{ticket_id}.",
        "processing_failed": "I couldn't process your message. Please send it again.",
        "unknown_command": "I don't know that command. Just describe the problem in your own words.",
        "start_known": "Just describe the problem and I'll file a ticket. /my — your tickets, /profile — change your room.",
        "my_empty": "No open tickets.",
        "my_header": "Your open tickets:",
    },
}

STATUS_NOTIFICATION = {
    TicketStatus.NEW: "ticket_new",
    TicketStatus.IN_PROGRESS: "ticket_in_progress",
    TicketStatus.DONE: "ticket_done",
    TicketStatus.CANCELLED: "ticket_cancelled",
}


class StatusCB(CallbackData, prefix="st"):
    ticket_id: int
    status: str


class AskCB(CallbackData, prefix="ask"):
    ticket_id: int


def say(key: str, language: Language, **kwargs: object) -> str:
    return TEXTS[language][key].format(**kwargs)


def language_from_code(code: str | None) -> Language:
    """`language_code` Telegram — тег вида `ru-RU`.

    Всё, кроме русского, обслуживаем по-английски: третий язык в контуре не заявлен,
    а молчаливый откат на русский для англоязычного сотрудника хуже английского для
    немецкого.
    """
    if code and code.split("-")[0].lower() == "ru":
        return Language.RU
    return Language.EN


def chunks(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет текст на части, влезающие в одно сообщение Telegram.

    Рвём по переводу строки, если он есть в пределах куска: склеенная заявка состоит
    из отдельных сообщений, и резать их посередине слова незачем.
    """
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        split_at = rest.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        parts.append(rest[:split_at])
        rest = rest[split_at:].lstrip("\n")
    parts.append(rest)
    return parts


def card_text(ticket: Ticket, *, display_name: str | None, limit: int) -> str:
    """Карточка заявки в группе очереди.

    Текст обрезается по N12: обращение на несколько экранов вытеснит кнопки за
    пределы видимого и упрётся в лимит сообщения Telegram. В БД лежит полный текст,
    он доступен по `/ticket`.
    """
    body = ticket.text
    tail = ""
    if len(body) > limit:
        body = body[:limit].rstrip()
        tail = f"\n…\nтекст целиком: /ticket {ticket.id}"

    header = (
        f"{STATUS_EMOJI[ticket.status]} <b>Заявка #{ticket.id}</b>\n"
        f"{escape(display_name or 'без имени')} · каб. {escape(ticket.room_snapshot)}"
        f" · {escape(ticket.department_snapshot)}\n"
        f"{ticket.created_at.strftime('%d.%m, %H:%M')}"
    )
    warning = "\n⚠️ В тексте был секрет — замаскирован" if ticket.security_flag else ""
    hint = "\n\n<i>Ответьте на это сообщение, чтобы задать вопрос заявителю.</i>"

    return f"{header}{warning}\n\n{escape(body)}{escape(tail)}{hint}"


def status_keyboard(ticket_id: int, status: TicketStatus) -> InlineKeyboardMarkup:
    """Кнопки под карточкой. Набор зависит от статуса: лишняя кнопка — лишняя ошибка."""
    if status in (TicketStatus.DONE, TicketStatus.CANCELLED):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вернуть в работу",
                        callback_data=StatusCB(
                            ticket_id=ticket_id, status=TicketStatus.IN_PROGRESS.value
                        ).pack(),
                    )
                ]
            ]
        )

    first: list[InlineKeyboardButton] = []
    if status is TicketStatus.NEW:
        first.append(
            InlineKeyboardButton(
                text="В работу",
                callback_data=StatusCB(
                    ticket_id=ticket_id, status=TicketStatus.IN_PROGRESS.value
                ).pack(),
            )
        )
    first.append(
        InlineKeyboardButton(
            text="Спросить", callback_data=AskCB(ticket_id=ticket_id).pack()
        )
    )

    second = [
        InlineKeyboardButton(
            text="Закрыть",
            callback_data=StatusCB(
                ticket_id=ticket_id, status=TicketStatus.DONE.value
            ).pack(),
        ),
        InlineKeyboardButton(
            text="Отменить",
            callback_data=StatusCB(
                ticket_id=ticket_id, status=TicketStatus.CANCELLED.value
            ).pack(),
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[first, second])


def departments_keyboard(departments: list[str]) -> InlineKeyboardMarkup:
    """Отделы по два в ряд (D10).

    Значение кнопки — индекс: название длиннее 64 байт в `callback_data` не поместится.
    """
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"dept:{index}")
        for index, name in enumerate(departments)
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_profile_keyboard(language: Language) -> InlineKeyboardMarkup:
    """Кнопка, с которой начинается анкета.

    Нужна, чтобы отделить ответ на вопрос анкеты от продолжения обращения: пока её
    не нажали, весь текст идёт в буфер.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=say("profile_start_button", language),
                    callback_data="profile:start",
                )
            ]
        ]
    )


def my_tickets_text(tickets: list[Ticket], language: Language) -> str:
    """Список для /my (F9). Текст режется до строки — это оглавление, а не карточка."""
    if not tickets:
        return say("my_empty", language)

    lines = [say("my_header", language)]
    for ticket in tickets:
        preview = ticket.text.splitlines()[0][:40]
        lines.append(
            f"{STATUS_EMOJI[ticket.status]} #{ticket.id} · "
            f"{ticket.created_at.strftime('%d.%m')} · {escape(preview)}…"
        )
    return "\n".join(lines)


def stats_text(summary: repo.Stats, days: int) -> str:
    """Сводка исполнителю (F11). Всегда по-русски: читает её один человек."""
    if summary.avg_seconds_to_close is None:
        average = "нет закрытых"
    else:
        hours, seconds = divmod(int(summary.avg_seconds_to_close), 3600)
        average = f"{hours} ч {seconds // 60} мин"

    lines = [
        f"<b>Сводка за {days} дн.</b>",
        f"Заявок: {summary.created}",
        f"Закрыто: {summary.closed}",
        f"Среднее до закрытия: {average}",
    ]
    if summary.top_requesters:
        top = ", ".join(
            f"{escape(name or telegram_id)} ({count})"
            for telegram_id, name, count in summary.top_requesters
        )
        lines.append(f"Чаще всех: {top}")
    return "\n".join(lines)
