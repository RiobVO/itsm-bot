"""Подключение к БД.

SQLite по умолчанию не проверяет внешние ключи — их нужно включать на каждом
соединении, иначе `ondelete` и ссылочная целостность в схеме остаются декорацией.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=echo)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(
    database_url: str, *, echo: bool = False
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_engine(database_url, echo=echo),
        expire_on_commit=False,
    )
