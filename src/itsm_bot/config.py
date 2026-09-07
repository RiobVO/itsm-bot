"""Настройки приложения.

Значения читаются из переменных окружения или `.env`. Секреты в коде не хранятся:
`.env` лежит вне репозитория, образец полей — в `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_DB_PATH = Path("itsm.db")


class DatabaseSettings(BaseSettings):
    """Только путь к БД.

    Отдельно от остальных настроек, потому что миграции запускаются там, где токена
    бота нет и быть не должно — в CI и при развёртывании до заполнения `.env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = DEFAULT_DB_PATH

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


class Settings(DatabaseSettings):
    """Полная конфигурация бота. Числовые значения — из раздела 2.2 TODO.md."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    bot_token: SecretStr
    queue_chat_id: int
    """Закрытая группа исполнителя, куда попадают карточки заявок (D9)."""

    departments: Annotated[list[str], NoDecode] = Field(min_length=1)
    """D10: отдел выбирается кнопками из этого списка, свободного ввода нет.

    `NoDecode` отключает разбор значения как JSON: без него pydantic-settings
    попытается прочитать `DEPARTMENTS` как JSON-массив и упадёт раньше валидатора.
    """

    card_text_limit: int = Field(default=1200, ge=200)
    """N12: сколько символов текста заявки помещается в карточку очереди."""

    buffer_seconds: int = Field(default=45, ge=5, le=300)
    """N3: сколько ждать после последнего сообщения, прежде чем собрать заявку."""

    rate_limit_per_hour: int = Field(default=5, ge=1)
    """N10: сколько заявок в час может создать один сотрудник."""

    dedup_window_minutes: int = Field(default=10, ge=1)
    """N11: за какое окно повторный одинаковый текст не создаёт вторую заявку."""

    @field_validator("departments", mode="before")
    @classmethod
    def _split_departments(cls, value: object) -> object:
        """Список из строки через запятую — этот файл правят руками, не программой."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз за процесс."""
    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
