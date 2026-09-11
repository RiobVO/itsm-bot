"""Тесты представления.

Карточка — единственное, на что исполнитель смотрит по двадцать раз в день, и
единственное место, где текст заявки целиком показать нельзя (N12).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from itsm_bot.bot import cards
from itsm_bot.storage.models import Language, Ticket, TicketStatus


def _ticket(**overrides: object) -> Ticket:
    values: dict[str, object] = {
        "id": 47,
        "requester_id": "42",
        "text": "Принтер не печатает",
        "status": TicketStatus.NEW,
        "room_snapshot": "312",
        "department_snapshot": "Бухгалтерия",
        "security_flag": False,
        "source_chat_id": 7,
        "source_message_id": 100,
        "text_hash": "x",
        "created_at": datetime(2026, 9, 14, 10, 24, tzinfo=UTC),
    }
    values.update(overrides)
    return Ticket(**values)


class TestLanguageDetection:
    @pytest.mark.parametrize("code", ["ru", "ru-RU", "RU"])
    def test_russian_codes_map_to_ru(self, code: str) -> None:
        assert cards.language_from_code(code) is Language.RU

    @pytest.mark.parametrize("code", ["en", "en-US", "de", None, ""])
    def test_everything_else_maps_to_en(self, code: str | None) -> None:
        assert cards.language_from_code(code) is Language.EN


class TestCardText:
    def test_card_shows_number_room_and_department(self) -> None:
        """Кабинет и отдел — первое, что ищет глазами исполнитель: он туда идёт."""
        text = cards.card_text(_ticket(), display_name="Пётр Иванов", limit=1200)

        assert "#47" in text
        assert "312" in text
        assert "Бухгалтерия" in text
        assert "Пётр Иванов" in text

    def test_status_emoji_is_first(self) -> None:
        """В списке сообщений группы видна только первая строка."""
        text = cards.card_text(_ticket(), display_name=None, limit=1200)

        assert text.startswith("🆕")

    def test_long_text_is_truncated(self) -> None:
        """N12: обращение на несколько экранов вытеснит кнопки за пределы видимого."""
        text = cards.card_text(_ticket(text="а" * 5000), display_name=None, limit=1200)

        assert "…" in text
        assert "а" * 1201 not in text
        assert "/ticket 47" in text

    def test_short_text_is_not_truncated(self) -> None:
        text = cards.card_text(_ticket(), display_name=None, limit=1200)

        assert "…" not in text
        assert "Принтер не печатает" in text

    def test_security_flag_adds_a_warning_line(self) -> None:
        text = cards.card_text(_ticket(security_flag=True), display_name=None, limit=1200)

        assert "⚠️" in text

    def test_angle_brackets_in_text_are_escaped(self) -> None:
        """Карточка уходит с HTML: неэкранированный текст просто не отправится."""
        text = cards.card_text(
            _ticket(text="что показывает <foo>"), display_name=None, limit=1200
        )

        assert "&lt;foo&gt;" in text
        assert "<foo>" not in text


class TestStatusKeyboard:
    def test_new_ticket_offers_taking_it(self) -> None:
        markup = cards.status_keyboard(47, TicketStatus.NEW)
        labels = [button.text for row in markup.inline_keyboard for button in row]

        assert "В работу" in labels

    def test_in_progress_does_not_offer_taking_it_again(self) -> None:
        markup = cards.status_keyboard(47, TicketStatus.IN_PROGRESS)
        labels = [button.text for row in markup.inline_keyboard for button in row]

        assert "В работу" not in labels
        assert "Закрыть" in labels

    def test_closed_ticket_offers_returning_to_work(self) -> None:
        markup = cards.status_keyboard(47, TicketStatus.DONE)
        labels = [button.text for row in markup.inline_keyboard for button in row]

        assert labels == ["Вернуть в работу"]

    def test_callback_data_round_trips(self) -> None:
        markup = cards.status_keyboard(47, TicketStatus.NEW)
        button = markup.inline_keyboard[0][0]

        parsed = cards.StatusCB.unpack(button.callback_data)

        assert parsed.ticket_id == 47
        assert parsed.status == TicketStatus.IN_PROGRESS.value


class TestDepartmentsKeyboard:
    def test_callback_carries_index_not_name(self) -> None:
        """Название отдела длиннее 64 байт в callback_data не поместится."""
        markup = cards.departments_keyboard(["Бухгалтерия", "Отдел кадров", "Продажи"])
        buttons = [button for row in markup.inline_keyboard for button in row]

        assert [button.callback_data for button in buttons] == [
            "dept:0",
            "dept:1",
            "dept:2",
        ]


class TestRequesterTexts:
    def test_notification_is_translated(self) -> None:
        russian = cards.say("ticket_done", Language.RU, ticket_id=47)
        english = cards.say("ticket_done", Language.EN, ticket_id=47)

        assert "47" in russian
        assert "47" in english
        assert russian != english

    def test_every_key_exists_in_both_languages(self) -> None:
        """Недостающий перевод не должен всплыть на живом боте."""
        assert set(cards.TEXTS[Language.RU]) == set(cards.TEXTS[Language.EN])

    def test_every_status_has_a_notification(self) -> None:
        """F7: смена статуса уведомляет заявителя, значит текст нужен для каждого."""
        assert set(cards.STATUS_NOTIFICATION) == set(TicketStatus)
        for key in cards.STATUS_NOTIFICATION.values():
            assert key in cards.TEXTS[Language.RU]

    def test_my_tickets_lists_numbers(self) -> None:
        text = cards.my_tickets_text([_ticket()], Language.RU)

        assert "#47" in text

    def test_my_tickets_empty_has_its_own_message(self) -> None:
        text = cards.my_tickets_text([], Language.RU)

        assert "#" not in text


class TestChunks:
    def test_short_text_stays_whole(self) -> None:
        assert cards.chunks("короткий") == ["короткий"]

    def test_long_text_is_split_under_the_limit(self) -> None:
        """Две склеенные простыни по 3000 символов — обычная заявка, а не край."""
        parts = cards.chunks("а" * 3000 + "\n" + "б" * 3000)

        assert len(parts) > 1
        assert all(len(part) <= cards.TELEGRAM_MESSAGE_LIMIT for part in parts)

    def test_split_prefers_line_breaks(self) -> None:
        parts = cards.chunks("раз\nдва\nтри", limit=8)

        assert parts[0] == "раз\nдва"

    def test_nothing_is_lost(self) -> None:
        text = "а" * 5000
        assert "".join(cards.chunks(text)) == text
