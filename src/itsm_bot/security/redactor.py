"""Маскирование секретов и персональных данных до отправки текста в модель.

Первый рубеж защиты. Всё, что идёт дальше по конвейеру — вызов модели, запись в БД,
логи, уведомления — работает только с результатом этой функции; сырой текст живёт
в памяти обработчика и никуда не сохраняется.

Правила намеренно осторожные: ложное срабатывание уродует описание заявки и мешает
исполнителю (стереть код ошибки хуже, чем пропустить сомнительную строку). Модель —
второй рубеж и ловит то, что не описывается регуляркой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECRET_PLACEHOLDER = "[REDACTED:secret]"
PII_PLACEHOLDER = "[REDACTED:pii]"

# Значение после слова «пароль» без разделителя маскируется, только если не похоже на
# продолжение фразы. Кириллица отсекается сразу, латинские частые слова — этим списком.
_PASSWORD_STOP_WORDS = frozenset(
    {
        "reset",
        "expired",
        "expires",
        "incorrect",
        "invalid",
        "wrong",
        "change",
        "changed",
        "forgot",
        "forgotten",
        "recovery",
        "policy",
        "manager",
        "required",
        "error",
        "issue",
        "problem",
        "please",
        "help",
        "from",
        "for",
        "and",
        "the",
        "not",
        "does",
        "is",
        "was",
        "to",
        "my",
    }
)
_MIN_PASSWORD_LENGTH = 4

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

_BEARER_TOKEN = re.compile(
    r"(?i)\b(bearer|api[_-]?key|apikey|access[_-]?token|secret[_-]?key|token|токен)\b"
    r"\s*[:=]?\s*"
    r"(?P<value>[A-Za-z0-9._\-]{12,})"
)

_PASSWORD = re.compile(
    r"(?i)\b(?P<keyword>парол\w*|пасс|password|passwd|pwd)\b"
    r"(?P<sep>\s*[:=]\s*|\s+(?:is|=)\s+|\s+)"
    r"(?P<value>\S+)"
)

_CONFIRMATION_CODE = re.compile(
    r"(?i)\b(?:код\s+(?:подтверждения|доступа|из\s+смс)|одноразовый\s+код|sms[-\s]?код"
    r"|otp|2fa|пин[-\s]?код|pin[-\s]?code|pin)\b\s*[:=№]?\s*(?P<value>\d{4,8})\b"
)

_CARD_CANDIDATE = re.compile(r"\b\d[\d \-]{11,21}\d\b")

_TAX_ID = re.compile(r"(?i)\b(?:инн|стир)\b\s*[:=№]?\s*(?P<value>\d{9,14})\b")

_PASSPORT = re.compile(
    r"(?i)\bпаспорт\w*\s*[:=№]?\s*(?P<value>[A-ZА-Я]{2}\s?\d{6,7}|\d{4}\s?\d{6})\b"
)

_BANK_ACCOUNT = re.compile(
    r"(?i)\b(?:р/с|расч[ёе]тный\s+сч[ёе]т|сч[ёе]т|account)\b\s*[:=№]?\s*(?P<value>\d{16,25})\b"
)


@dataclass(frozen=True)
class RedactionResult:
    """Замаскированный текст и то, что в нём нашлось."""

    text: str
    secret_found: bool
    pii_found: bool

    @property
    def found(self) -> bool:
        return self.secret_found or self.pii_found


def redact(text: str) -> RedactionResult:
    """Заменяет секреты и персональные данные плейсхолдерами.

    Порядок важен: структурные форматы (JWT, номера карт) разбираются раньше правил по
    ключевым словам, иначе ключевое слово съест часть длинного значения.
    """
    secret_found = False
    pii_found = False

    text, hit = _mask_jwt(text)
    secret_found |= hit

    text, hit = _mask_cards(text)
    pii_found |= hit

    text, hit = _mask_by_pattern(_BEARER_TOKEN, text, SECRET_PLACEHOLDER)
    secret_found |= hit

    text, hit = _mask_passwords(text)
    secret_found |= hit

    text, hit = _mask_by_pattern(_CONFIRMATION_CODE, text, SECRET_PLACEHOLDER)
    secret_found |= hit

    for pattern in (_TAX_ID, _PASSPORT, _BANK_ACCOUNT):
        text, hit = _mask_by_pattern(pattern, text, PII_PLACEHOLDER)
        pii_found |= hit

    return RedactionResult(text=text, secret_found=secret_found, pii_found=pii_found)


def _mask_jwt(text: str) -> tuple[str, bool]:
    masked, count = _JWT.subn(SECRET_PLACEHOLDER, text)
    return masked, count > 0


def _mask_by_pattern(pattern: re.Pattern[str], text: str, placeholder: str) -> tuple[str, bool]:
    """Заменяет группу `value`, сохраняя окружающий текст (ключевое слово остаётся)."""
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        found = True
        start, end = match.span("value")
        return match.group(0)[: start - match.start()] + placeholder + match.group(0)[end - match.start() :]

    return pattern.sub(replace, text), found


def _mask_passwords(text: str) -> tuple[str, bool]:
    """Маскирует значение пароля, отличая его от продолжения фразы.

    С явным разделителем (`пароль: X`) значение маскируется всегда. Без разделителя —
    только если слово не выглядит частью предложения, иначе «пароль не подходит»
    превратилось бы в «пароль [REDACTED:secret] подходит».
    """
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        value = match.group("value")
        has_separator = ":" in match.group("sep") or "=" in match.group("sep")

        if not (has_separator or _looks_like_password(value)):
            return match.group(0)

        found = True
        return f"{match.group('keyword')}{match.group('sep')}{SECRET_PLACEHOLDER}"

    return _PASSWORD.sub(replace, text), found


def _looks_like_password(value: str) -> bool:
    if value == SECRET_PLACEHOLDER:
        return False
    if len(value) < _MIN_PASSWORD_LENGTH:
        return False
    if not value.isascii():
        return False
    return value.strip(".,!?;:").lower() not in _PASSWORD_STOP_WORDS


def _mask_cards(text: str) -> tuple[str, bool]:
    """Маскирует номера карт, проверяя контрольную сумму Луна.

    Без этой проверки под шаблон попадают инвентарные номера, серийники и телефоны —
    а они исполнителю нужны.
    """
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        digits = re.sub(r"\D", "", match.group(0))
        if not (13 <= len(digits) <= 19 and _luhn_valid(digits)):
            return match.group(0)
        found = True
        return PII_PLACEHOLDER

    return _CARD_CANDIDATE.sub(replace, text), found


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
