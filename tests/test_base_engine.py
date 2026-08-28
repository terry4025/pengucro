import asyncio
import time

from engines.base_engine import BaseEngine, _is_benign_proactor_teardown


class DummyEngine(BaseEngine):
    def make_reservation_thread(self, reservation_data):
        self.notify_success()

    async def make_reservation_async_task(self, reservation_data, task_idx):
        await asyncio.sleep(0.01)
        self.notify_success()


def test_proactor_filter_suppresses_only_known_winerror_10022_teardown():
    benign = OSError("invalid argument")
    benign.winerror = 10022
    other = OSError("connection reset")
    other.winerror = 10054

    assert _is_benign_proactor_teardown(
        {"exception": benign, "handle": "_call_connection_lost(None)"}
    ) is True
    assert _is_benign_proactor_teardown(
        {"exception": benign, "handle": "different_callback()"}
    ) is False
    assert _is_benign_proactor_teardown(
        {"exception": other, "handle": "_call_connection_lost(None)"}
    ) is False

def test_success_callback_fires_once_across_workers():
    successes = []
    engine = DummyEngine(lambda *_: None, lambda: successes.append(True))
    engine.start_reservation({}, 5, is_async=True)
    deadline = time.time() + 2
    while engine.is_running and time.time() < deadline:
        time.sleep(0.01)
    assert successes == [True]
    assert engine.is_running is False
