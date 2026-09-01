"""Тесты маскирования. Задают границу: что обязано быть скрыто и что обязано остаться.

Ложное срабатывание здесь не безобидно — оно уродует описание заявки, поэтому
негативных кейсов столько же, сколько позитивных.
"""

import pytest

from itsm_bot.security.redactor import PII_PLACEHOLDER, SECRET_PLACEHOLDER, redact


class TestPasswords:
    def test_password_with_separator_is_masked(self):
        result = redact("Не могу войти, пароль: Zavod2024!")

        assert "Zavod2024!" not in result.text
        assert SECRET_PLACEHOLDER in result.text
        assert result.secret_found is True

    def test_password_without_separator_is_masked(self):
        result = redact("Мой пароль Zavod2024! логин a.karimov. Сбросьте пожалуйста")

        assert "Zavod2024!" not in result.text
        assert "a.karimov" in result.text, "логин — не секрет, он нужен исполнителю"

    def test_english_password_keyword_is_masked(self):
        result = redact("my password is Hunter2000")

        assert "Hunter2000" not in result.text
        assert result.secret_found is True

    def test_lowercase_word_password_is_masked(self):
        result = redact("пароль qwerty")

        assert "qwerty" not in result.text

    @pytest.mark.parametrize(
        "text",
        [
            "Пароль не подходит, пишет неверный",
            "Забыл пароль от почты",
            "password reset требуется",
            "Пароль истёк, что делать",
            "нужно сбросить пароль",
        ],
    )
    def test_password_mentioned_without_value_is_untouched(self, text):
        result = redact(text)

        assert result.text == text
        assert result.secret_found is False


class TestTokens:
    def test_jwt_is_masked(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
            ".dQw4w9WgXcQ1234567890abcdefghij"
        )
        result = redact(f"Сервис отдаёт ошибку с токеном {jwt} в заголовке")

        assert jwt not in result.text
        assert result.secret_found is True

    def test_bearer_token_is_masked(self):
        result = redact("Передаю Bearer sk-ant-abcdef1234567890 для интеграции")

        assert "sk-ant-abcdef1234567890" not in result.text
        assert result.secret_found is True

    def test_api_key_is_masked(self):
        result = redact("api_key=A1b2C3d4E5f6G7h8")

        assert "A1b2C3d4E5f6G7h8" not in result.text


class TestCards:
    def test_valid_card_number_is_masked_as_pii(self):
        result = redact("Оплата не прошла картой 4111 1111 1111 1111")

        assert "4111" not in result.text
        assert PII_PLACEHOLDER in result.text
        assert result.pii_found is True

    def test_card_without_separators_is_masked(self):
        result = redact("списали с 4242424242424242 дважды")

        assert "4242424242424242" not in result.text

    def test_invalid_checksum_is_not_a_card(self):
        # Инвентарные номера и серийники не должны исчезать из заявки.
        text = "Инвентарный номер 1234 5678 9012 3456 на корпусе"
        result = redact(text)

        assert result.text == text
        assert result.pii_found is False

    def test_phone_number_is_untouched(self):
        text = "Мой номер +998 90 123 45 67, звоните"
        result = redact(text)

        assert result.text == text


class TestConfirmationCodes:
    def test_confirmation_code_is_masked(self):
        result = redact("Код подтверждения 482913 не приходит")

        assert "482913" not in result.text
        assert result.secret_found is True

    def test_error_code_is_untouched(self):
        # Самое дорогое ложное срабатывание: без кода ошибки заявка теряет смысл.
        text = "1С выдаёт код ошибки 404 при подключении"
        result = redact(text)

        assert result.text == text
        assert result.secret_found is False


class TestPersonalData:
    def test_tax_id_is_masked(self):
        result = redact("Мой ИНН 123456789 не проходит в системе")

        assert "123456789" not in result.text
        assert result.pii_found is True

    def test_passport_is_masked(self):
        result = redact("Паспорт AA1234567 не читается сканером")

        assert "AA1234567" not in result.text
        assert result.pii_found is True

    def test_bank_account_is_masked(self):
        result = redact("Счёт 20208000900001234567 отображается неверно")

        assert "20208000900001234567" not in result.text
        assert result.pii_found is True


class TestCleanText:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Не работает принтер на 3 этаже",
            "Monitor flickers, office 214",
            "1С не открывается с утра, ошибка соединения с сервером",
        ],
    )
    def test_clean_text_passes_through_unchanged(self, text):
        result = redact(text)

        assert result.text == text
        assert result.found is False


class TestCombined:
    def test_both_kinds_are_flagged(self):
        result = redact("пароль: Zavod2024! и карта 4111 1111 1111 1111")

        assert result.secret_found is True
        assert result.pii_found is True
        assert SECRET_PLACEHOLDER in result.text
        assert PII_PLACEHOLDER in result.text

    def test_redaction_is_idempotent(self):
        once = redact("Мой пароль Zavod2024!")
        twice = redact(once.text)

        assert twice.text == once.text
        assert twice.found is False, "плейсхолдер не должен маскироваться повторно"
