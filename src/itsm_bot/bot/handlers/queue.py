"""Группа очереди: кнопки статусов (F6, F7) и вопрос заявителю (F8).

Вопрос набирается reply на карточку: `reply_to_message.message_id` совпадает с
`queue_message_id`, и привязка к заявке получается без состояния в памяти — порядок
вопросов по разным заявкам любой, перезапуск процесса ничего не рвёт (D9).

Инвариант D15 держится здесь: второй вопрос тому же человеку, пока первый без
ответа, отклоняется. Иначе его сообщение станет двусмысленным, и понадобятся кнопки
выбора заявки со своим хранением сырого текста и своим жизненным циклом.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from itsm_bot.bot import cards
from itsm_bot.config import Settings
from itsm_bot.storage import repo
from itsm_bot.storage.models import Language, MessageDirection, Ticket, TicketStatus

logger = logging.getLogger(__name__)

router = Router(name="queue")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
"""Фильтр на уровне роутера, а не проверка внутри хендлера.

Хендлер, который «просто вернул управление», всё равно считается сработавшим, и
апдейт до `messages.router` не дойдёт. Без этого фильтра ответ заявителя reply на
сообщение бота в личке проглатывался бы здесь и не попадал бы ни в заявку, ни в
буфер."""


@router.callback_query(cards.AskCB.filter())
async def ask_hint(callback: CallbackQuery) -> None:
    """Кнопка только подсказывает: сам вопрос набирается reply на карточку."""
    await callback.answer(
        "Ответьте на эту карточку своим сообщением — вопрос уйдёт заявителю.",
        show_alert=True,
    )


@router.callback_query(cards.StatusCB.filter())
async def change_status(
    callback: CallbackQuery,
    callback_data: cards.StatusCB,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    try:
        status = TicketStatus(callback_data.status)
    except ValueError:
        logger.warning("Неизвестный статус в колбэке: %s", callback_data.status)
        await callback.answer("Неизвестный статус", show_alert=True)
        return

    ticket = await repo.set_status(session, callback_data.ticket_id, status)
    if ticket is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    employee = await repo.get_employee(session, ticket.requester_id)
    text = cards.card_text(
        ticket,
        display_name=employee.display_name if employee else None,
        limit=settings.card_text_limit,
    )

    # Статус уже записан: БД коммитится до правки карточки и до уведомления, и
    # откатить её здесь нечем. Поэтому каждый оставшийся шаг сообщает о своём сбое
    # отдельно — расхождение между БД, карточкой и уведомлением должно быть видно
    # исполнителю, а не только в логе.
    try:
        await callback.message.edit_text(
            text, reply_markup=cards.status_keyboard(ticket.id, ticket.status)
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            # Прочие 400 — «сообщение нельзя редактировать», ошибки разметки — это
            # настоящий сбой: БД изменилась, карточка нет, и молчать о нём нельзя.
            logger.exception("Не удалось перерисовать карточку заявки #%s", ticket.id)
            await callback.answer(
                f"Статус #{ticket.id} изменён, но карточка не обновилась",
                show_alert=True,
            )
        else:
            # Карточка уже в этом виде: два нажатия подряд — обычное дело.
            logger.info("Карточка заявки #%s не изменилась", ticket.id)
    except Exception:
        logger.exception("Не удалось перерисовать карточку заявки #%s", ticket.id)
        await callback.answer(
            f"Статус #{ticket.id} изменён, но карточка не обновилась", show_alert=True
        )

    try:
        await bot.send_message(
            ticket.source_chat_id,
            cards.say(
                cards.STATUS_NOTIFICATION[ticket.status],
                employee.language if employee else Language.RU,
                ticket_id=ticket.id,
            ),
        )
    except Exception:
        logger.exception("Не удалось уведомить заявителя по заявке #%s", ticket.id)
        await callback.message.reply(
            f"Статус #{ticket.id} изменён, но уведомление заявителю не ушло."
        )

    await callback.answer()


@router.message(F.reply_to_message, F.text, ~F.text.startswith("/"))
async def ask_requester(
    message: Message, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    """Reply на карточку — вопрос заявителю (F8).

    Команды исключены фильтром: `queue.router` подключён раньше `commands.router`, и
    без этого `/queue` реплаем на карточку ушёл бы заявителю как вопрос и завёл бы
    состояние ожидания ответа.
    """
    if message.chat.id != settings.queue_chat_id:
        return

    statement = select(Ticket).where(
        Ticket.queue_message_id == message.reply_to_message.message_id
    )
    ticket = (await session.execute(statement)).scalar_one_or_none()
    if ticket is None:
        await message.reply("Это сообщение не привязано к заявке.")
        return

    if ticket.status in repo.CLOSED_STATUSES:
        # Закрытая заявка не ждёт ответа, и ответ на такой вопрос уехал бы в новую
        # заявку, пока исполнитель считает, что спросил.
        await message.reply(
            f"Заявка #{ticket.id} закрыта. Верните её в работу, чтобы задать вопрос."
        )
        return

    waiting = await repo.tickets_awaiting_answer(
        session, requester_id=ticket.requester_id
    )
    other = [item.id for item in waiting if item.id != ticket.id]
    if other:
        numbers = ", ".join(f"#{number}" for number in other)
        await message.reply(
            f"У этого сотрудника уже есть неотвеченный вопрос: {numbers}. "
            "Дождитесь ответа или задайте всё одним сообщением."
        )
        return

    employee = await repo.get_employee(session, ticket.requester_id)

    # Сначала доставка, потом запись. Обратный порядок оставил бы в БД «ждём ответа»
    # по вопросу, которого заявитель не получал, и следующее его сообщение подшилось
    # бы ответом на несуществующий вопрос.
    try:
        await bot.send_message(
            ticket.source_chat_id,
            cards.say(
                "question",
                employee.language if employee else Language.RU,
                ticket_id=ticket.id,
                text=message.text,
            ),
            # Текст исполнителя уходит как есть: при HTML вопрос вида «что показывает
            # <foo>» не отправился бы вовсе.
            parse_mode=None,
        )
    except Exception:
        logger.exception("Не удалось доставить вопрос по заявке #%s", ticket.id)
        await message.reply("Не смог доставить вопрос заявителю, попробуйте ещё раз.")
        return

    try:
        await repo.add_message(
            session, ticket.id, MessageDirection.TO_REQUESTER, message.text
        )
    except Exception:
        # Вопрос доставлен, но заявка не помечена ждущей: ответ придёт новой заявкой.
        # Молчать здесь нельзя — исполнитель должен знать, что состояние разошлось.
        logger.exception("Вопрос по заявке #%s доставлен, но не записан", ticket.id)
        await message.reply(
            f"Вопрос по #{ticket.id} доставлен, но не записан в заявку. "
            "Ответ придёт отдельным обращением."
        )
        return

    await message.reply(f"Вопрос отправлен по заявке #{ticket.id}.")
