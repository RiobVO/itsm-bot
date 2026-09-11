"""Точка входа: python -m itsm_bot.bot"""

from __future__ import annotations

import asyncio

from itsm_bot.bot.main import run

if __name__ == "__main__":
    asyncio.run(run())
