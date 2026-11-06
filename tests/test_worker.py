import asyncio

import pytest

from argus.config import Settings
from argus.worker.main import run_worker

pytestmark = pytest.mark.integration


async def test_worker_stops_on_signal(settings: Settings) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings, stop, heartbeat_s=0.05))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
