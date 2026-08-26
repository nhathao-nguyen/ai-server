import asyncio
from pathlib import Path

import pytest

from app.core.database import Database
from app.scheduler.gpu_lock import GpuLock
from app.scheduler.jobs import JobSpec
from app.scheduler.manager import JobManager


@pytest.mark.asyncio
async def test_cancelled_submit_interrupts_running_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "server.db")
    db.initialize()
    manager = JobManager(db, GpuLock(1), max_cpu_queue=2, max_gpu_queue=2)
    started = asyncio.Event()

    async def runner(_spec):
        started.set()
        await asyncio.sleep(60)

    await manager.start()
    task = asyncio.create_task(
        manager.submit(JobSpec("test", "test", "test", None, False), runner)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    row = db.fetch_one("SELECT state, error_code FROM jobs")
    assert row["state"] == "interrupted"
    assert row["error_code"] == "client_disconnected"
    await manager.stop()
    db.close()
