import asyncio
import inspect
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from app.core.database import Database
from app.core.errors import ApiError
from app.scheduler.gpu_lock import GpuLock
from app.scheduler.jobs import JobSpec


Runner = Callable[[JobSpec], Awaitable[Any]]
StreamProducer = Callable[[JobSpec], AsyncIterator[Any]]
QueueItem = tuple[str, JobSpec, Runner, asyncio.Future[Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LaneQueue:
    def __init__(self) -> None:
        self._items: deque[QueueItem] = deque()
        self._condition = asyncio.Condition()
        self._unfinished = 0
        self._drained = asyncio.Event()
        self._drained.set()

    async def put(self, item: QueueItem) -> None:
        async with self._condition:
            self._items.append(item)
            self._unfinished += 1
            self._drained.clear()
            self._condition.notify()

    async def get_next(self, owner: str | None, consecutive: int, batch_size: int) -> QueueItem:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            index = self._select_index(owner, consecutive, batch_size)
            item = self._items[index]
            del self._items[index]
            return item

    def _select_index(self, owner: str | None, consecutive: int, batch_size: int) -> int:
        if owner is None:
            return 0
        if consecutive >= batch_size:
            for index, item in enumerate(self._items):
                if item[1].provider != owner:
                    return index
        for index, item in enumerate(self._items):
            if item[1].provider == owner:
                return index
        return 0

    def task_done(self) -> None:
        self._unfinished -= 1
        if self._unfinished < 0:
            raise ValueError("task_done() called too many times")
        if self._unfinished == 0:
            self._drained.set()

    async def join(self) -> None:
        await self._drained.wait()

    def qsize(self) -> int:
        return len(self._items)


class JobManager:
    """Run CPU and GPU jobs on independent lanes with bounded GPU affinity."""

    def __init__(
        self,
        db: Database,
        gpu_lock: GpuLock,
        *,
        cpu_provider: Any | None = None,
        idle_sleep_seconds: float = 3600.0,
        idle_clock: Callable[[], float] | None = None,
        idle_reaper_interval_seconds: float = 60.0,
        gpu_affinity_batch_size: int = 2,
        gpu_workers: int | None = None,
        max_gpu_queue: int = 16,
        max_cpu_queue: int = 16,
        max_concurrent_jobs_per_key: int = 2,
    ) -> None:
        if idle_sleep_seconds <= 0:
            raise ValueError("MODEL_IDLE_SLEEP_SECONDS must be greater than zero")
        if idle_reaper_interval_seconds <= 0:
            raise ValueError("IDLE_REAPER_INTERVAL_SECONDS must be greater than zero")
        if gpu_affinity_batch_size < 1:
            raise ValueError("GPU_AFFINITY_BATCH_SIZE must be at least one")
        if max_gpu_queue < 1 or max_cpu_queue < 1:
            raise ValueError("queue limits must be at least one")
        if max_concurrent_jobs_per_key < 1:
            raise ValueError("MAX_CONCURRENT_JOBS_PER_KEY must be at least one")
        requested_gpu_workers = gpu_lock.max_jobs if gpu_workers is None else gpu_workers
        if not 1 <= requested_gpu_workers <= gpu_lock.max_jobs:
            raise ValueError("gpu_workers must be between 1 and max_gpu_ai_jobs")
        self.db = db
        self.gpu_lock = gpu_lock
        self.cpu_provider = cpu_provider
        self.idle_sleep_seconds = idle_sleep_seconds
        self.idle_reaper_interval_seconds = idle_reaper_interval_seconds
        self.gpu_affinity_batch_size = gpu_affinity_batch_size
        self.gpu_workers = requested_gpu_workers
        self.max_gpu_queue = max_gpu_queue
        self.max_cpu_queue = max_cpu_queue
        self.max_concurrent_jobs_per_key = max_concurrent_jobs_per_key
        self._clock = idle_clock or time.monotonic
        self._cpu_queue: asyncio.Queue[QueueItem | None] = asyncio.Queue()
        self._gpu_queue = _LaneQueue()
        self._cpu_worker: asyncio.Task | None = None
        self._reaper: asyncio.Task | None = None
        self._gpu_workers: list[asyncio.Task] = []
        self._gpu_selection_lock = asyncio.Lock()
        self._gpu_last_provider: str | None = None
        self._gpu_consecutive = 0
        self._started = False
        self._stopping = False
        self._running_cpu = 0
        self._running_gpu = 0
        self._last_activity = self._clock()
        self._idle_lock = asyncio.Lock()
        self._key_condition = asyncio.Condition()
        self._key_active: dict[str, int] = {}
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        self._enqueue_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        self._last_activity = self._clock()
        self._cpu_worker = asyncio.create_task(self._run_cpu(), name="tts-server-cpu-worker")
        self._gpu_workers = [
            asyncio.create_task(self._run_gpu(), name=f"tts-server-gpu-worker-{index}")
            for index in range(self.gpu_workers)
        ]
        self._reaper = asyncio.create_task(self._run_reaper(), name="tts-server-idle-reaper")

    async def submit(self, spec: JobSpec, runner: Runner) -> Any:
        job_id, future = await self._enqueue(spec, runner)
        try:
            return await future
        except asyncio.CancelledError:
            self._cancel_job(job_id, future)
            raise

    async def submit_stream(self, spec: JobSpec, producer: StreamProducer) -> AsyncIterator[Any]:
        """Submit a streaming job and expose bounded, incremental output."""

        channel: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=32)

        def signal_terminal(kind: str) -> None:
            try:
                channel.put_nowait((kind, None))
                return
            except asyncio.QueueFull:
                # Preserve the terminal marker for a consumer that is still
                # draining after a producer error. The channel is bounded, so
                # evict one buffered chunk rather than blocking cleanup.
                try:
                    channel.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    channel.put_nowait((kind, None))
                except asyncio.QueueFull:
                    pass

        async def runner(_spec: JobSpec) -> None:
            try:
                async for item in producer(_spec):
                    await channel.put(("item", item))
            except BaseException:
                signal_terminal("error")
                raise
            await channel.put(("done", None))

        job_id, future = await self._enqueue(spec, runner)

        async def iterator() -> AsyncIterator[Any]:
            completed = False
            try:
                while True:
                    kind, item = await channel.get()
                    if kind == "item":
                        yield item
                        continue
                    if kind == "error":
                        await future
                    else:
                        await future
                        completed = True
                        return
            finally:
                if not completed and not future.done():
                    self._cancel_job(job_id, future)

        return iterator()

    async def _enqueue(self, spec: JobSpec, runner: Runner) -> tuple[str, asyncio.Future[Any]]:
        if not self._started or self._stopping:
            raise RuntimeError("JobManager is not started")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        job_id = uuid.uuid4().hex
        async with self._enqueue_lock:
            self._last_activity = self._clock()
            queue_limit = self.max_gpu_queue if spec.requires_gpu else self.max_cpu_queue
            queue_depth = self._gpu_queue.qsize() if spec.requires_gpu else self._cpu_queue.qsize()
            if queue_depth >= queue_limit:
                raise ApiError(
                    "queue_full",
                    "The requested execution queue is full",
                    429,
                    {"retry_after": 1, "lane": "gpu" if spec.requires_gpu else "cpu"},
                )
            self.db.execute(
                """
                INSERT INTO jobs (id, api_key_id, kind, provider, model, state, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, spec.api_key_id, spec.kind, spec.provider, spec.model, _now(), json.dumps(spec.metadata, default=str)),
            )
            item = (job_id, spec, runner, future)
            if spec.requires_gpu:
                await self._gpu_queue.put(item)
            else:
                await self._cpu_queue.put(item)
        return job_id, future

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        await self._cpu_queue.join()
        await self._gpu_queue.join()
        await self._cpu_queue.put(None)
        if self._reaper is not None:
            self._reaper.cancel()
        if self._cpu_worker is not None:
            await self._cpu_worker
        for worker in self._gpu_workers:
            worker.cancel()
        if self._gpu_workers:
            await asyncio.gather(*self._gpu_workers, return_exceptions=True)
        if self._reaper is not None:
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        await self.gpu_lock.sleep()
        await self._stop_cpu_provider()
        self._cpu_worker = None
        self._gpu_workers = []
        self._reaper = None
        self._started = False

    async def reset_runtime(self) -> None:
        """Drain queued work and release resident providers without touching SQLite."""

        if not self._started:
            return
        await self._cpu_queue.join()
        await self._gpu_queue.join()
        await self.gpu_lock.sleep()
        await self._stop_cpu_provider()
        self._last_activity = self._clock()

    async def reap_idle(self, now: float | None = None) -> bool:
        current = self._clock() if now is None else now
        if current - self._last_activity < self.idle_sleep_seconds:
            return False
        if self._cpu_queue.qsize() or self._gpu_queue.qsize() or self._running_cpu or self._running_gpu:
            return False
        async with self._idle_lock:
            current = self._clock() if now is None else now
            if current - self._last_activity < self.idle_sleep_seconds:
                return False
            if self._cpu_queue.qsize() or self._gpu_queue.qsize() or self._running_cpu or self._running_gpu:
                return False
            gpu_slept = await self.gpu_lock.sleep()
            cpu_stopped = await self._stop_cpu_provider()
            self._last_activity = current
            return gpu_slept or cpu_stopped

    def get_status(self) -> dict[str, Any]:
        queued = self._cpu_queue.qsize() + self._gpu_queue.qsize()
        running = self._running_cpu + self._running_gpu
        return {
            "queued": int(queued),
            "running": int(running),
            "queue_depth": self._cpu_queue.qsize() + self._gpu_queue.qsize(),
            "cpu_queue_depth": self._cpu_queue.qsize(),
            "gpu_queue_depth": self._gpu_queue.qsize(),
            "cpu_active": self._running_cpu,
            "gpu_active": self.gpu_lock.active_jobs,
            "gpu_capacity": self.gpu_lock.max_jobs,
            "gpu_workers": self.gpu_workers,
            "gpu_waiting": self.gpu_lock.waiting_jobs,
            "gpu_owner": self.gpu_lock.owner,
            "gpu_switching": self.gpu_lock.switching,
            "provider_lifecycle": self.gpu_lock.provider_lifecycle,
        }

    async def _run_cpu(self) -> None:
        while True:
            item = await self._cpu_queue.get()
            if item is None:
                self._cpu_queue.task_done()
                return
            self._running_cpu += 1
            try:
                await self._run_execution(item, gpu=False)
            finally:
                self._cpu_queue.task_done()

    async def _run_gpu(self) -> None:
        while True:
            async with self._gpu_selection_lock:
                item = await self._gpu_queue.get_next(
                    self.gpu_lock.owner,
                    self._gpu_consecutive,
                    self.gpu_affinity_batch_size,
                )
                provider = item[1].provider
                if provider == self._gpu_last_provider:
                    self._gpu_consecutive += 1
                else:
                    self._gpu_last_provider = provider
                    self._gpu_consecutive = 1
            self._running_gpu += 1
            try:
                await self._run_execution(item, gpu=True)
            finally:
                self._gpu_queue.task_done()

    async def _run_execution(self, item: QueueItem, *, gpu: bool) -> None:
        job_id = item[0]
        task = asyncio.create_task(self._execute(item, gpu=gpu), name=f"tts-server-job-{job_id}")
        self._execution_tasks[job_id] = task
        outcome: tuple[bool, Any] | None = None
        try:
            outcome = await task
        except asyncio.CancelledError:
            # A client can cancel a streaming execution without taking down
            # the lane worker. Shutdown drains the queues before cancelling
            # workers, so this is also safe during normal stop().
            pass
        finally:
            self._execution_tasks.pop(job_id, None)
            if gpu:
                self._running_gpu -= 1
            else:
                self._running_cpu -= 1
        if outcome is not None:
            future = item[3]
            succeeded, value = outcome
            if not future.done():
                if succeeded:
                    future.set_result(value)
                else:
                    future.set_exception(value)

    async def _execute(self, item: QueueItem, *, gpu: bool) -> tuple[bool, Any] | None:
        job_id, spec, runner, future = item
        if future.cancelled():
            self.db.execute(
                "UPDATE jobs SET state = 'interrupted', finished_at = ?, error_code = ?, error_message = ? WHERE id = ?",
                (_now(), "client_disconnected", "Streaming client disconnected before execution", job_id),
            )
            return None
        self.db.execute(
            "UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?",
            (_now(), job_id),
        )
        key_acquired = False
        try:
            key_acquired = await self._acquire_key(spec.api_key_id)
            if gpu:
                async with self.gpu_lock.acquire(spec.provider):
                    result = await runner(spec)
            else:
                result = await runner(spec)
        except asyncio.CancelledError:
            self.db.execute(
                "UPDATE jobs SET state = 'interrupted', finished_at = ?, error_code = ?, error_message = ? WHERE id = ?",
                (_now(), "client_disconnected", "Streaming client disconnected", job_id),
            )
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            error_message = getattr(exc, "message", str(exc))
            self.db.execute(
                "UPDATE jobs SET state = 'failed', finished_at = ?, error_code = ?, error_message = ? WHERE id = ?",
                (_now(), error_code, error_message[:500], job_id),
            )
            return False, exc
        else:
            self.db.execute(
                "UPDATE jobs SET state = 'succeeded', finished_at = ? WHERE id = ?",
                (_now(), job_id),
            )
            return True, result
        finally:
            if key_acquired:
                await self._release_key(spec.api_key_id)

    def _cancel_job(self, job_id: str, future: asyncio.Future[Any]) -> None:
        if not future.done():
            future.cancel()
        task = self._execution_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()

    async def _run_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.idle_reaper_interval_seconds)
                try:
                    await self.reap_idle()
                except Exception:
                    # A provider failure must not kill the scheduler. The next
                    # request can retry its lazy load, and shutdown still runs.
                    continue
        except asyncio.CancelledError:
            raise

    async def _stop_cpu_provider(self) -> bool:
        provider = self.cpu_provider
        if provider is None:
            return False
        for name in ("stop", "unload"):
            method = getattr(provider, name, None)
            if method is None:
                continue
            result = method()
            if inspect.isawaitable(result):
                await result
            return True
        return False

    async def _acquire_key(self, api_key_id: str | None) -> bool:
        if api_key_id is None:
            return False
        async with self._key_condition:
            while self._key_active.get(api_key_id, 0) >= self.max_concurrent_jobs_per_key:
                await self._key_condition.wait()
            self._key_active[api_key_id] = self._key_active.get(api_key_id, 0) + 1
            return True

    async def _release_key(self, api_key_id: str | None) -> None:
        if api_key_id is None:
            return
        async with self._key_condition:
            active = self._key_active.get(api_key_id, 0) - 1
            if active <= 0:
                self._key_active.pop(api_key_id, None)
            else:
                self._key_active[api_key_id] = active
            self._key_condition.notify_all()
