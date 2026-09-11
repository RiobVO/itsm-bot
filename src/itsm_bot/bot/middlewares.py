"""Проводка зависимостей в хендлеры.

Сессия открывается на апдейт и закрывается вместе с ним: при 5 обращениях в день
(N2) держать её дольше незачем, а закрытая сессия не даст отложенному коду тихо
дочитать заявку.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_bot.bot.buffer import MessageBuffer
from itsm_bot.bot.intake import Intake
from itsm_bot.config import Settings


@dataclass
class ServicesMiddleware(BaseMiddleware):
    session_factory: async_sessionmaker[AsyncSession]
    intake: Intake
    buffer: MessageBuffer
    settings: Settings
    publish: Callable[[int], Awaitable[int | None]]

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            data["intake"] = self.intake
            data["buffer"] = self.buffer
            data["settings"] = self.settings
            data["publish"] = self.publish
            return await handler(event, data)
