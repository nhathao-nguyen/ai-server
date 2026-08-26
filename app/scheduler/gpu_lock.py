import asyncio
import inspect
from contextlib import asynccontextmanager
from collections import defaultdict
from typing import Any, AsyncIterator

from app.engines.base import ProviderStatus


class GpuLock:
    """Bound GPU work and make provider ownership explicit.

    Same-provider jobs may share one loaded provider up to ``max_jobs``. A
    different provider cannot activate until all active jobs have completed and
    the previous provider has been unloaded and confirmed. A provider is
    optional so the scheduler remains useful with lightweight test runners and
    unavailable optional dependencies.
    """

    def __init__(self, max_jobs: int = 1, *, switch_unload: bool = True) -> None:
        if not 1 <= max_jobs <= 4:
            raise ValueError("MAX_GPU_AI_JOBS must be between 1 and 4")
        self.max_jobs = max_jobs
        self.switch_unload = switch_unload
        self._semaphore = asyncio.Semaphore(max_jobs)
        self._active_jobs = 0
        self._waiting_jobs = 0
        self._providers: dict[str, Any] = {}
        self._owner: str | None = None
        self._switching = False
        self._owner_lock = asyncio.Lock()
        self._active_zero = asyncio.Event()
        self._active_zero.set()
        self._provider_lifecycle: dict[str, dict[str, int]] = defaultdict(
            lambda: {"load": 0, "unload": 0, "restart": 0}
        )

    @property
    def active_jobs(self) -> int:
        return self._active_jobs

    @property
    def waiting_jobs(self) -> int:
        return self._waiting_jobs

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def switching(self) -> bool:
        return self._switching

    @property
    def resident_owners(self) -> list[str]:
        return [self._owner] if self._owner is not None else []

    @property
    def provider_lifecycle(self) -> dict[str, dict[str, int]]:
        return {provider: dict(values) for provider, values in self._provider_lifecycle.items()}

    def register_provider(self, owner: str, provider: Any) -> None:
        self._providers[owner] = provider

    @asynccontextmanager
    async def acquire(self, provider: str | None = None) -> AsyncIterator[None]:
        self._waiting_jobs += 1
        waiting = True
        acquired_slot = False
        active = False
        try:
            await self._semaphore.acquire()
            acquired_slot = True
            if provider is not None:
                await self.ensure_owner(provider)
            self._waiting_jobs -= 1
            waiting = False
            self._active_jobs += 1
            self._active_zero.clear()
            active = True
            yield
        finally:
            if waiting:
                self._waiting_jobs -= 1
            if active:
                self._active_jobs -= 1
                if self._active_jobs == 0:
                    self._active_zero.set()
            if acquired_slot:
                self._semaphore.release()

    async def ensure_owner(self, owner: str) -> None:
        async with self._owner_lock:
            if self._owner == owner:
                return
            await self._active_zero.wait()
            self._switching = True
            try:
                if self._owner is not None:
                    if not self.switch_unload:
                        raise RuntimeError("GPU provider switch requires GPU_SWITCH_UNLOAD=true")
                    old_owner = self._owner
                    await self._unload(old_owner)
                    await self._confirm_unloaded(old_owner)
                    self._owner = None
                await self._activate(owner)
                self._owner = owner
            finally:
                self._switching = False

    async def sleep(self) -> bool:
        """Unload the current GPU owner once no GPU job is active."""

        async with self._owner_lock:
            await self._active_zero.wait()
            if self._owner is None:
                return False
            self._switching = True
            try:
                owner = self._owner
                await self._unload(owner)
                await self._confirm_unloaded(owner)
                self._owner = None
                return True
            finally:
                self._switching = False

    async def capability_probe(self, owner: str) -> ProviderStatus:
        """Run a provider capability probe only while no GPU owner is resident."""

        provider = self._providers.get(owner)
        if provider is None:
            return ProviderStatus(False, "provider_missing")
        method = getattr(provider, "capability_status", None)
        if not callable(method):
            return ProviderStatus(True)
        async with self._owner_lock:
            if self._owner is not None:
                return ProviderStatus(
                    True,
                    details={"worker_probe": "deferred_gpu_owner", "gpu_owner": self._owner},
                )
            await self._active_zero.wait()
            self._switching = True
            try:
                result = method()
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, ProviderStatus):
                    return result
                return ProviderStatus(False, "worker_probe_invalid")
            except Exception as exc:
                return ProviderStatus(False, "worker_probe_failed", {"error": type(exc).__name__})
            finally:
                self._switching = False

    async def _activate(self, owner: str) -> None:
        provider = self._providers.get(owner)
        if provider is None:
            return
        await _invoke(provider, ("activate", "start"))
        self._provider_lifecycle[owner]["load"] += 1

    async def _unload(self, owner: str) -> None:
        provider = self._providers.get(owner)
        if provider is None:
            return
        await _invoke(provider, ("unload", "stop"))
        self._provider_lifecycle[owner]["unload"] += 1

    async def _confirm_unloaded(self, owner: str) -> None:
        provider = self._providers.get(owner)
        if provider is None:
            return
        await _invoke(provider, ("confirm_unloaded",))


async def _invoke(provider: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        method = getattr(provider, name, None)
        if method is None:
            continue
        result = method()
        if inspect.isawaitable(result):
            return await result
        return result
    return None
