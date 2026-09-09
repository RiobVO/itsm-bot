"""Тесты буфера склейки (F5, N3, D13).

Окно в тестах — доли секунды: проверяется поведение, а не 45 секунд из N3.
Каждый тест, который мог бы зависнуть, обёрнут в `asyncio.wait_for`: дедлок должен
падать по таймауту, а не вешать весь прогон.
"""

from __future__ import annotations

import asyncio

from itsm_bot.bot.buffer import Batch, MessageBuffer

WINDOW = 0.05


def _collector() -> tuple[list[Batch], object]:
    collected: list[Batch] = []

    async def flush(batch: Batch) -> None:
        collected.append(batch)

    return collected, flush


class TestGluing:
    async def test_three_messages_become_one_batch(self) -> None:
        """F5: «Здравствуйте» → «принтер» → «не печатает» — это одно обращение."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        for index, line in enumerate(["Здравствуйте", "у меня принтер", "не печатает"]):
            await buffer.add("42", text=line, chat_id=7, message_id=100 + index)
        await asyncio.sleep(WINDOW * 4)

        assert len(collected) == 1
        assert collected[0].text == "Здравствуйте\nу меня принтер\nне печатает"

    async def test_batch_keeps_first_message_id(self) -> None:
        """На первое сообщение пачки отвечаем заявителю — оно и попадает в заявку."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="раз", chat_id=7, message_id=100)
        await buffer.add("42", text="два", chat_id=7, message_id=101)
        await asyncio.sleep(WINDOW * 4)

        assert collected[0].first_message_id == 100

    async def test_each_message_restarts_the_window(self) -> None:
        """Окно отсчитывается от последнего сообщения, а не от первого (N3)."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="раз", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 0.6)
        await buffer.add("42", text="два", chat_id=7, message_id=101)
        await asyncio.sleep(WINDOW * 0.6)

        assert collected == []

        await asyncio.sleep(WINDOW * 4)
        assert len(collected) == 1

    async def test_pause_longer_than_window_splits_batches(self) -> None:
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="раз", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 4)
        await buffer.add("42", text="два", chat_id=7, message_id=200)
        await asyncio.sleep(WINDOW * 4)

        assert [item.text for item in collected] == ["раз", "два"]


class TestTimerDoesNotCancelItself:
    """Таймерная задача не должна отменять сама себя перед вызовом обработчика.

    Обработчик обязан содержать `await` до записи результата — иначе тест пройдёт и
    на сломанной реализации: `CancelledError` доставляется на ближайшей точке
    переключения, а обработчик без единого `await` успевает отработать целиком.
    У настоящего `intake` такие точки есть на каждом обращении к БД.
    """

    async def test_flush_survives_an_await_before_recording(self) -> None:
        collected: list[Batch] = []

        async def flush(batch: Batch) -> None:
            await asyncio.sleep(0)
            collected.append(batch)

        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 4)

        assert [item.text for item in collected] == ["принтер"]

    async def test_flush_runs_to_completion_after_several_awaits(self) -> None:
        """Обработка пачки — это несколько запросов к БД подряд, а не один."""
        stages: list[str] = []

        async def flush(batch: Batch) -> None:
            for stage in ("profile", "duplicate", "rate_limit", "insert"):
                await asyncio.sleep(0)
                stages.append(stage)

        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 4)

        assert stages == ["profile", "duplicate", "rate_limit", "insert"]


class TestLockIsNotHeldDuringFlush:
    """Ревью этапа 2: `flush` вызывает `intake`, который берёт ту же блокировку."""

    async def test_flush_may_take_the_same_lock(self) -> None:
        done = asyncio.Event()
        buffer: MessageBuffer

        async def flush(batch: Batch) -> None:
            async with buffer.lock(batch.telegram_id):
                done.set()

        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)

        await asyncio.wait_for(done.wait(), timeout=2)

    async def test_release_may_take_the_same_lock(self) -> None:
        done = asyncio.Event()
        buffer: MessageBuffer

        async def flush(batch: Batch) -> None:
            async with buffer.lock(batch.telegram_id):
                done.set()

        buffer = MessageBuffer(window_seconds=10, flush=flush)
        buffer.hold("42")
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)

        await asyncio.wait_for(buffer.release("42"), timeout=2)

        assert done.is_set()


class TestIsolation:
    async def test_different_employees_do_not_mix(self) -> None:
        """Исполнитель один, а пишут из разных отделов одновременно."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await buffer.add("99", text="мышь", chat_id=8, message_id=200)
        await asyncio.sleep(WINDOW * 4)

        assert {(item.telegram_id, item.text) for item in collected} == {
            ("42", "принтер"),
            ("99", "мышь"),
        }

    async def test_locks_are_per_employee(self) -> None:
        buffer = MessageBuffer(window_seconds=WINDOW, flush=_collector()[1])

        assert buffer.lock("42") is buffer.lock("42")
        assert buffer.lock("42") is not buffer.lock("99")


class TestHold:
    """Пока идёт анкета, окно не тикает — иначе первое обращение уедет без профиля."""

    async def test_held_buffer_does_not_flush_on_timer(self) -> None:
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        buffer.hold("42")
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 6)

        assert collected == []

    async def test_hold_after_add_cancels_the_running_timer(self) -> None:
        """Сообщение пришло раньше, чем хендлер успел поставить удержание."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 0.4)
        buffer.hold("42")
        await asyncio.sleep(WINDOW * 6)

        assert collected == []

    async def test_release_flushes_immediately(self) -> None:
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=10, flush=flush)

        buffer.hold("42")
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await buffer.release("42")

        assert [item.text for item in collected] == ["принтер"]

    async def test_release_without_pending_is_silent(self) -> None:
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=10, flush=flush)

        buffer.hold("42")
        await buffer.release("42")

        assert collected == []

    async def test_messages_during_hold_are_glued_together(self) -> None:
        """Человек дописывает подробности, пока отвечает на анкету."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        buffer.hold("42")
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 3)
        await buffer.add("42", text="не печатает", chat_id=7, message_id=101)
        await buffer.release("42")

        assert [item.text for item in collected] == ["принтер\nне печатает"]

    async def test_hold_of_one_employee_does_not_stop_another(self) -> None:
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        buffer.hold("42")
        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await buffer.add("99", text="мышь", chat_id=8, message_id=200)
        await asyncio.sleep(WINDOW * 4)

        assert [item.telegram_id for item in collected] == ["99"]

    async def test_release_resumes_normal_gluing(self) -> None:
        """После анкеты буфер обязан снова работать по таймеру, а не остаться немым."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        buffer.hold("42")
        await buffer.add("42", text="первое", chat_id=7, message_id=100)
        await buffer.release("42")

        await buffer.add("42", text="второе", chat_id=7, message_id=101)
        await asyncio.sleep(WINDOW * 4)

        assert [item.text for item in collected] == ["первое", "второе"]


class TestClose:
    async def test_close_cancels_timers_without_flushing(self) -> None:
        """Остановка процесса не досылает недособранное: текст сырой, и его некуда девать (D13)."""
        collected, flush = _collector()
        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)

        await buffer.add("42", text="принтер", chat_id=7, message_id=100)
        await buffer.close()
        await asyncio.sleep(WINDOW * 4)

        assert collected == []


class TestFlushFailure:
    async def test_failed_flush_does_not_keep_the_batch(self) -> None:
        """Исключение в обработке не должно приклеить старый текст к следующему обращению."""
        attempts: list[Batch] = []

        async def flush(batch: Batch) -> None:
            attempts.append(batch)
            raise RuntimeError("Telegram недоступен")

        buffer = MessageBuffer(window_seconds=WINDOW, flush=flush)
        await buffer.add("42", text="раз", chat_id=7, message_id=100)
        await asyncio.sleep(WINDOW * 4)
        await buffer.add("42", text="два", chat_id=7, message_id=101)
        await asyncio.sleep(WINDOW * 4)

        assert [item.text for item in attempts] == ["раз", "два"]
