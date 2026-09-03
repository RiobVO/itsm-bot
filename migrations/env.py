"""Точка входа alembic.

Две вещи отличают этот файл от шаблонного:

- URL берётся из настроек приложения, а не из `alembic.ini`. Путь к БД задан в одном
  месте, и миграция не может уехать не в ту базу из-за рассинхрона двух конфигов.
- `render_as_batch=True`. SQLite не умеет менять и удалять колонки через ALTER TABLE;
  без батчевого режима первая же миграция, правящая существующую колонку, упадёт.
- `render_item` разворачивает собственные типы в их эквивалент из SQLAlchemy, чтобы
  готовая миграция не импортировала код приложения.
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from itsm_bot.config import DatabaseSettings
from itsm_bot.storage.models import Base, UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", DatabaseSettings().database_url)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Рендерит `UtcDateTime` как обычный `DateTime`.

    В DDL это одно и то же — разница только в обработке значений на стороне Python.
    Зато миграция перестаёт зависеть от модуля приложения: она должна применяться и
    через год, когда класс успеют переименовать или убрать.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к БД."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
