"""Проверка миграций — критерий приёмки этапа 1 в TODO.md.

Тест синхронный намеренно: `migrations/env.py` сам вызывает `asyncio.run()`, и
внутри уже работающего цикла событий он бы упал.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from itsm_bot.storage.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TABLES = {"employees", "tickets", "ticket_messages"}


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Alembic, направленный на одноразовую БД: env.py берёт путь из DB_PATH."""
    db_path = tmp_path / "migration.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.chdir(PROJECT_ROOT)

    config = Config(PROJECT_ROOT / "alembic.ini")
    config.attributes["db_path"] = db_path
    return config


def _sync_url(config: Config) -> str:
    return f"sqlite:///{config.attributes['db_path']}"


def test_upgrade_creates_all_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_url(alembic_config))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLES <= tables


def test_migration_matches_models(alembic_config: Config) -> None:
    """Схема после миграции совпадает с моделями.

    Ловит расхождение, из-за которого тесты идут по `create_all`, бот — по
    миграциям, и на живой БД не хватает колонки.
    """
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_url(alembic_config))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True}
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == [], f"Миграция разошлась с моделями: {differences}"


def test_downgrade_removes_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    engine = create_engine(_sync_url(alembic_config))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert not (EXPECTED_TABLES & tables)


def test_upgrade_is_repeatable_after_downgrade(alembic_config: Config) -> None:
    """Миграция обратима: применяется, откатывается и применяется снова."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(_sync_url(alembic_config))
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLES <= tables
