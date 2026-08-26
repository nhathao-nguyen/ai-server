import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from app.core.errors import ApiError
from app.core.model_manifest import validate_manifest
from app.engines.base import ProviderStatus, TtsRequest, TtsResult
from app.engines.tts_options import effective_voice, normalize_tts_options, validate_tts_request
from app.workers.protocol import redact_worker_text


LOGGER = logging.getLogger("tts_server.worker")


class VieneuProvider:
    def __init__(
        self,
        cache_dir: Path | str,
        python_executable: str | None = None,
        manifest_path: Path | str | None = None,
        timeout_seconds: float = 900.0,
        cache_only: bool = True,
        runtime_config: dict | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.python_executable = python_executable or sys.executable
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.timeout_seconds = timeout_seconds
        self.cache_only = cache_only
        self.temp_dir = Path(temp_dir) if temp_dir is not None else self.cache_dir.parent
        self.runtime_config = {
            "mode": "v3turbo",
            "device": "cpu",
            "backend": "onnx",
            "precision": "int8",
            "threads": 0,
            "max_batch_size": 32,
            **(runtime_config or {}),
        }
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._runtime_state = "available"

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    def dependency_available(self) -> bool:
        _prefer_virtualenv_site(self.python_executable)
        return importlib.util.find_spec("vieneu") is not None

    def status(self) -> ProviderStatus:
        if not self.dependency_available():
            return ProviderStatus(False, "dependency_missing")
        if self.manifest_path is None:
            return ProviderStatus(True)
        if not self.manifest_path.exists():
            return ProviderStatus(False, "checkpoint_missing", {"manifest": str(self.manifest_path)})
        valid, reason, details = validate_manifest(
            self.manifest_path,
            "tts-vietnamese",
            "vieneu",
            cache_root=self.cache_dir,
        )
        if not valid:
            return ProviderStatus(False, reason, {**details, "manifest": str(self.manifest_path)})
        return ProviderStatus(True, details=details)

    async def activate(self) -> None:
        await self.start()

    async def start(self) -> None:
        status = self.status()
        if not status.available:
            raise ApiError("vieneu_unavailable", f"VieNeu is unavailable: {status.reason}", 503, status.details)
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["HF_HOME"] = str(self.cache_dir)
            env["HUGGINGFACE_HUB_CACHE"] = str(self.cache_dir)
            env["PYTHONUTF8"] = "1"
            env["TTS_VIENEU_CONFIG"] = json.dumps(self.runtime_config, ensure_ascii=False)
            if self.cache_only:
                env["HF_HUB_OFFLINE"] = "1"
                env["TRANSFORMERS_OFFLINE"] = "1"
                env["HF_DATASETS_OFFLINE"] = "1"
            self._runtime_state = "warming"
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.python_executable,
                    "-m",
                    "app.workers.vieneu_worker",
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._stderr_task = asyncio.create_task(self._forward_stderr(self._process))
                line = await asyncio.wait_for(self._process.stdout.readline(), self.timeout_seconds)
            except asyncio.CancelledError:
                await asyncio.shield(self.stop())
                self._runtime_state = "unavailable"
                raise
            except (OSError, asyncio.TimeoutError) as exc:
                await self.stop()
                self._runtime_state = "unavailable"
                raise ApiError("vieneu_worker_error", "VieNeu worker could not start", 503) from exc
            try:
                payload = _decode_line(line)
            except ApiError:
                await self.stop()
                self._runtime_state = "unavailable"
                raise
            if not payload.get("ready"):
                await self.stop()
                self._runtime_state = "unavailable"
                raise ApiError("vieneu_worker_error", "VieNeu worker did not become ready", 503, payload)
            self._runtime_state = "loaded"

    async def generate(self, request: TtsRequest) -> TtsResult:
        validate_tts_request("vieneu", request)
        try:
            await self.start()
        except asyncio.CancelledError:
            await asyncio.shield(self.stop())
            raise
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ApiError("vieneu_worker_error", "VieNeu worker is not available", 503)
        async with self._request_lock:
            started = time.perf_counter()
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="vieneu-job-", dir=str(self.temp_dir)) as temp_dir:
                output_path = Path(temp_dir) / "output.wav"
                payload = {
                    "text": request.text,
                    "voice": request.voice,
                    "reference_audio_path": request.reference_audio_path,
                    "reference_transcript": request.reference_transcript,
                    "output_path": str(output_path),
                    "speed": request.speed,
                    "options": request.options,
                }
                try:
                    self._process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                    await self._process.stdin.drain()
                    line = await asyncio.wait_for(self._process.stdout.readline(), self.timeout_seconds)
                except asyncio.CancelledError:
                    await asyncio.shield(self.stop())
                    self._runtime_state = "unavailable"
                    raise
                except asyncio.TimeoutError as exc:
                    await self.stop()
                    self._runtime_state = "unavailable"
                    raise ApiError("vieneu_timeout", "VieNeu generation timed out", 504) from exc
                except (BrokenPipeError, ConnectionError, OSError, RuntimeError) as exc:
                    await self.stop()
                    self._runtime_state = "unavailable"
                    raise ApiError("vieneu_worker_error", "VieNeu worker connection failed", 503) from exc
                try:
                    result = _decode_line(line)
                except ApiError:
                    await self.stop()
                    self._runtime_state = "unavailable"
                    raise
                if not result.get("ok") or not output_path.exists():
                    await self.stop()
                    self._runtime_state = "unavailable"
                    raise ApiError("vieneu_generation_failed", "VieNeu generation failed", 502, {"error": result.get("error")})
                self._runtime_state = "loaded"
                return TtsResult(
                    audio=output_path.read_bytes(),
                    sample_rate=int(result.get("sample_rate") or 48000),
                    audio_duration_ms=int(result.get("audio_duration_ms") or 0),
                    processing_ms=round((time.perf_counter() - started) * 1000),
                    model="tts-vietnamese",
                    voice=request.voice,
                    metadata={
                        "provider": "vieneu",
                        "voice": result.get("voice") or effective_voice("vieneu", request),
                        "reference_mode": "clone" if request.reference_audio_path else "preset",
                        "effective_options": result.get("effective_options") or normalize_tts_options("vieneu", request.options),
                        "speed": request.speed,
                        "reference_transcript_ignored": bool(request.reference_transcript),
                    },
                )

    async def stop(self) -> None:
        process = self._process
        self._process = None
        self._runtime_state = "sleeping"
        if process is None:
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                self._stderr_task = None
            return
        if process.returncode is None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except RuntimeError:
                    pass
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def unload(self) -> None:
        await self.stop()

    async def confirm_unloaded(self) -> None:
        if self._process is not None and self._process.returncode is None:
            raise RuntimeError("VieNeu worker is still resident")

    async def _forward_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                message = line.decode("utf-8", errors="replace").strip()
                if message:
                    LOGGER.warning("component=vieneu-worker stderr=%s", redact_worker_text(message))
        except asyncio.CancelledError:
            raise


def _decode_line(line: bytes) -> dict:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("vieneu_invalid_worker_response", "VieNeu worker returned invalid data", 502) from exc
    if not isinstance(value, dict):
        raise ApiError("vieneu_invalid_worker_response", "VieNeu worker returned invalid data", 502)
    return value


def _prefer_virtualenv_site(python_executable: str) -> None:
    site_packages = Path(python_executable).resolve().parents[1] / "Lib" / "site-packages"
    site_package_text = str(site_packages)
    if site_packages.is_dir():
        sys.path[:] = [entry for entry in sys.path if entry != site_package_text]
        sys.path.insert(0, site_package_text)
