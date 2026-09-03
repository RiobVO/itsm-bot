"""Общие фикстуры.

БД в тестах — настоящий файл в tmp_path, а не `:memory:`: in-memory SQLite живёт
внутри одного соединения, и поведение пула в таком тесте расходится с боевым.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_bot.storage.models import Base
from itsm_bot.storage.session import create_engine


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()
