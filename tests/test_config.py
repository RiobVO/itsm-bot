"""Тесты настроек.

Список отделов приходит из окружения строкой через запятую: JSON в `.env`
неудобно править руками, а править его будут при каждом изменении оргструктуры.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from itsm_bot.config import Settings


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Окружение без `.env` проекта: chdir уводит pydantic-settings от рабочего файла."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("QUEUE_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("DEPARTMENTS", "Бухгалтерия, Отдел кадров ,Продажи")
    return monkeypatch


class TestDepartments:
    def test_comma_separated_string_becomes_list(self, env: pytest.MonkeyPatch) -> None:
        assert Settings().departments == ["Бухгалтерия", "Отдел кадров", "Продажи"]

    def test_blank_items_are_dropped(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("DEPARTMENTS", "ИТ,,Продажи,")

        assert Settings().departments == ["ИТ", "Продажи"]

    def test_empty_directory_is_rejected(self, env: pytest.MonkeyPatch) -> None:
        """Пустой справочник — анкета не соберётся, и это должно падать на старте."""
        env.setenv("DEPARTMENTS", " , ")

        with pytest.raises(ValueError):
            Settings()

    def test_missing_directory_is_rejected(self, env: pytest.MonkeyPatch) -> None:
        env.delenv("DEPARTMENTS")

        with pytest.raises(ValueError):
            Settings()


class TestCardTextLimit:
    def test_default_matches_n12(self, env: pytest.MonkeyPatch) -> None:
        assert Settings().card_text_limit == 1200
