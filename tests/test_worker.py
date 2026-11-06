import asyncio

import pytest

from argus.config import Settings
from argus.worker.main import run_worker

pytestmark = pytest.mark.integration


async def test_worker_starts_and_stops_on_signal(settings: Settings) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings.model_copy(update={"worker_id": "t"}), stop))
    await asyncio.sleep(0.3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)
