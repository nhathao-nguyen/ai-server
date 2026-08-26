import ast
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


class ChatterboxProvider:
    def __init__(
        self,
        cache_dir: Path | str,
        python_executable: str | None = None,
        manifest_path: Path | str | None = None,
        timeout_seconds: float = 900.0,
        cache_only: bool = True,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.python_executable = python_executable or sys.executable
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.timeout_seconds = timeout_seconds
        self.cache_only = cache_only
        self.temp_dir = Path(temp_dir) if temp_dir is not None else self.cache_dir.parent
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._runtime_state = "available"
        self._capability_probe: ProviderStatus | None = None

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    def dependency_available(self) -> bool:
        _prefer_virtualenv_site(self.python_executable)
        return importlib.util.find_spec("chatterbox") is not None

    def v3_api_available(self) -> bool:
        if not self.dependency_available():
            return False
        try:
            package_spec = importlib.util.find_spec("chatterbox")
            locations = list(package_spec.submodule_search_locations or []) if package_spec else []
            source_path = Path(locations[0]) / "mtl_tts.py" if locations else None
            if source_path is None or not source_path.exists():
                return False
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or node.name != "ChatterboxMultilingualTTS":
                    continue
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == "from_pretrained":
                        names = {arg.arg for arg in (*method.args.args, *method.args.kwonlyargs)}
                        return "t3_model" in names
        except (OSError, SyntaxError, AttributeError, ValueError):
            pass
        return False

    def cuda_runtime_available(self) -> bool:
        """Check the actual torch CUDA capability, not only package presence."""

        try:
            _prefer_virtualenv_site(self.python_executable)
            import torch

            return bool(torch.version.cuda and torch.cuda.is_available())
        except (ImportError, OSError, RuntimeError):
            return False

    def status(self) -> ProviderStatus:
        if not self.dependency_available():
            return ProviderStatus(False, "dependency_missing")
        if not self.v3_api_available():
            return ProviderStatus(False, "v3_api_incompatible", {"required": "from_pretrained(t3_model='v3')"})
        if self.manifest_path is not None and not self.manifest_path.exists():
            return ProviderStatus(False, "checkpoint_missing", {"manifest": str(self.manifest_path)})
        if not self.cuda_runtime_available():
            return ProviderStatus(False, "cuda_runtime_unavailable", {"required": "torch.version.cuda and torch.cuda.is_available()"})
        manifest_status = self._checkpoint_manifest_status()
        if not manifest_status.available:
            return manifest_status
        return ProviderStatus(True, details=manifest_status.details)

    def _checkpoint_manifest_status(self) -> ProviderStatus:
        if self.manifest_path is None:
            return ProviderStatus(True)
        valid, reason, details = validate_manifest(
            self.manifest_path,
            "tts-multilingual",
            "chatterbox",
            cache_root=self.cache_dir,
        )
        return ProviderStatus(valid, reason, details)

    async def capability_status(self) -> ProviderStatus:
        """Return static readiness plus one bounded, cache-only worker load probe."""

        static = self.status()
        if not static.available:
            return static
        if self._capability_probe is not None:
            if self._process is not None and self._process.returncode is not None:
                self._capability_probe = None
            else:
                return self._capability_probe

        probe_timeout = min(max(self.timeout_seconds, 0.1), 60.0)
        result: ProviderStatus | None = None
        try:
            await asyncio.wait_for(self.start(), timeout=probe_timeout)
            result = ProviderStatus(True, details={**static.details, "worker_probe": "pass"})
        except asyncio.TimeoutError:
            result = ProviderStatus(
                False,
                "worker_probe_timeout",
                {**static.details, "worker_probe": "timeout", "timeout_seconds": probe_timeout},
            )
        except ApiError as exc:
            result = ProviderStatus(
                False,
                "worker_probe_failed",
                {**static.details, "worker_probe": "failed", "error": exc.code},
            )
        except Exception as exc:
            result = ProviderStatus(
                False,
                "worker_probe_failed",
                {**static.details, "worker_probe": "failed", "error": type(exc).__name__},
            )
        finally:
            try:
                await self.stop()
            except Exception:
                if result is None or result.available:
                    result = ProviderStatus(
                        False,
                        "worker_probe_failed",
                        {**static.details, "worker_probe": "failed", "error": "worker_stop_failed"},
                    )

        self._capability_probe = result or ProviderStatus(
            False,
            "worker_probe_failed",
            {**static.details, "worker_probe": "failed"},
        )
        return self._capability_probe

    async def activate(self) -> None:
        await self.start()

    async def start(self) -> None:
        status = self.status()
        if not status.available:
            raise ApiError("chatterbox_unavailable", f"Chatterbox is unavailable: {status.reason}", 503, status.details)
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["HF_HOME"] = str(self.cache_dir)
            env["HUGGINGFACE_HUB_CACHE"] = str(self.cache_dir)
            env["PYTHONUTF8"] = "1"
            if self.cache_only:
                env["HF_HUB_OFFLINE"] = "1"
                env["TRANSFORMERS_OFFLINE"] = "1"
                env["HF_DATASETS_OFFLINE"] = "1"
            self._runtime_state = "warming"
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.python_executable,
                    "-m",
                    "app.workers.chatterbox_worker",
                    "--persistent",
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._stderr_task = asyncio.create_task(self._forward_stderr(self._process))
                line = await asyncio.wait_for(self._process.stdout.readline(), self.timeout_seconds)
                payload = _decode_line(line)
            except asyncio.CancelledError:
                await asyncio.shield(self.stop())
                self._runtime_state = "unavailable"
                self._capability_probe = None
                raise
            except asyncio.TimeoutError as exc:
                await self.stop()
                self._runtime_state = "unavailable"
                self._capability_probe = None
                raise ApiError("chatterbox_timeout", "Chatterbox worker did not become ready", 504) from exc
            except (OSError, ApiError) as exc:
                await self.stop()
                self._runtime_state = "unavailable"
                self._capability_probe = None
                if isinstance(exc, ApiError):
                    raise
                raise ApiError("chatterbox_worker_error", "Chatterbox worker could not start", 503) from exc
            if not payload.get("ready"):
                await self.stop()
                self._runtime_state = "unavailable"
                self._capability_probe = None
                raise ApiError("chatterbox_worker_error", "Chatterbox worker did not become ready", 503, payload)
            self._runtime_state = "loaded"

    async def generate(self, request: TtsRequest) -> TtsResult:
        validate_tts_request("chatterbox", request)
        try:
            await self.start()
        except asyncio.CancelledError:
            await asyncio.shield(self.stop())
            raise
        async with self._request_lock:
            if self._process is None or self._process.stdin is None or self._process.stdout is None:
                raise ApiError("chatterbox_worker_error", "Chatterbox worker is not available", 503)
            started = time.perf_counter()
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="chatterbox-job-", dir=str(self.temp_dir)) as temp_dir:
                output_path = Path(temp_dir) / "output.wav"
                payload = {
                    "text": request.text,
                    "language": request.language,
                    "voice": request.voice,
                    "reference_audio_path": request.reference_audio_path,
                    "reference_transcript": request.reference_transcript,
                    "options": request.options,
                    "speed": request.speed,
                    "output_path": str(output_path),
                }
                try:
                    self._process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                    await self._process.stdin.drain()
                    line = await asyncio.wait_for(self._process.stdout.readline(), self.timeout_seconds)
                    result = _decode_line(line)
                except asyncio.CancelledError:
                    await asyncio.shield(self.stop())
                    self._runtime_state = "unavailable"
                    self._capability_probe = None
                    raise
                except asyncio.TimeoutError as exc:
                    await self.stop()
                    self._runtime_state = "unavailable"
                    self._capability_probe = None
                    raise ApiError("chatterbox_timeout", "Chatterbox generation timed out", 504) from exc
                except (BrokenPipeError, ConnectionError, OSError, ApiError) as exc:
                    await self.stop()
                    self._runtime_state = "unavailable"
                    self._capability_probe = None
                    if isinstance(exc, ApiError):
                        raise
                    raise ApiError("chatterbox_worker_error", "Chatterbox worker failed", 503) from exc
                if not result.get("ok") or not output_path.exists():
                    await self.stop()
                    self._runtime_state = "unavailable"
                    self._capability_probe = None
                    raise ApiError(
                        "chatterbox_generation_failed",
                        "Chatterbox generation failed",
                        502,
                        {"error": result.get("error")},
                    )
                self._runtime_state = "loaded"
                return TtsResult(
                    audio=output_path.read_bytes(),
                    sample_rate=int(result.get("sample_rate") or 24000),
                    audio_duration_ms=int(result.get("audio_duration_ms") or 0),
                    processing_ms=round((time.perf_counter() - started) * 1000),
                    model="tts-multilingual",
                    voice=request.voice,
                    metadata={
                        "provider": "chatterbox",
                        "voice": result.get("voice") or effective_voice("chatterbox", request),
                        "reference_mode": "clone" if request.reference_audio_path else "built_in",
                        "effective_options": result.get("effective_options") or normalize_tts_options("chatterbox", request.options),
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
            raise RuntimeError("Chatterbox worker is still resident")

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
                    LOGGER.warning("component=chatterbox-worker stderr=%s", redact_worker_text(message))
        except asyncio.CancelledError:
            raise


def _decode_line(line: bytes) -> dict:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("chatterbox_invalid_worker_response", "Chatterbox worker returned invalid data", 502) from exc
    if not isinstance(value, dict):
        raise ApiError("chatterbox_invalid_worker_response", "Chatterbox worker returned invalid data", 502)
    return value


def _prefer_virtualenv_site(python_executable: str) -> None:
    site_packages = Path(python_executable).resolve().parents[1] / "Lib" / "site-packages"
    site_package_text = str(site_packages)
    if site_packages.is_dir():
        sys.path[:] = [entry for entry in sys.path if entry != site_package_text]
        sys.path.insert(0, site_package_text)


def _worker_error(payload: dict | None, stderr: bytes) -> str:
    payload_error = str(payload.get("error")) if payload and payload.get("error") else ""
    stderr_tail = stderr.decode("utf-8", errors="replace").strip()[-300:]
    if payload_error and stderr_tail:
        return f"{payload_error}; stderr: {stderr_tail}"[:500]
    return (payload_error or stderr_tail or "worker_exited_without_output")[:500]
