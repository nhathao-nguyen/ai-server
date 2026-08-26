import argparse
import asyncio
import contextlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from app.core.config import Settings
from app.core.model_manifest import ModelManifest
from app.core.paths import AppPaths
from app.engines.base import LlmRequest, TtsRequest
from app.engines.chatterbox import ChatterboxProvider
from app.engines.ollama import OllamaProvider
from app.engines.vieneu import VieneuProvider


@dataclass(frozen=True)
class BootstrapResult:
    model: str
    status: str
    action: str
    reason: str | None = None
    digest: str | None = None
    bytes: int | None = None
    cache_path: str | None = None
    manifest_path: str | None = None
    smoke_test: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class BootstrapRunner:
    CHATTERBOX_SOURCE = "git+https://github.com/resemble-ai/chatterbox.git"
    PYTORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu124"
    PYTORCH_CUDA_PACKAGES = ("torch==2.6.0+cu124", "torchaudio==2.6.0+cu124")

    def __init__(self, settings: Settings, paths: AppPaths, ollama_client=None) -> None:
        self.settings = settings
        self.paths = paths
        self.ollama_client = ollama_client or OllamaProvider(
            settings.ollama_url,
            settings.ollama_model,
            settings.gpu_job_timeout_seconds,
        )
        self.ollama_injected = ollama_client is not None

    def result(self, model: str, status: str, reason: str | None = None, **kwargs) -> BootstrapResult:
        return BootstrapResult(model=model, status=status, action=status, reason=reason, **kwargs)

    def run_all(self) -> list[BootstrapResult]:
        self.paths.ensure_directories()
        try:
            with self._exclusive_lock(self.paths.locks / "model-bootstrap.lock"):
                results: list[BootstrapResult] = []
                for model, ensure in (
                    ("ollama", self.ensure_ollama),
                    ("chatterbox", self.ensure_chatterbox),
                    ("vieneu", self.ensure_vieneu),
                ):
                    try:
                        results.append(ensure())
                    except Exception as exc:
                        results.append(self.result(model, "failed", f"{type(exc).__name__}:{str(exc)[:240]}"))
                return results
        except OSError as exc:
            return [
                self.result(model, "blocked", "bootstrap_locked")
                for model in ("ollama", "chatterbox", "vieneu")
            ]

    def ensure_ollama(self) -> BootstrapResult:
        executable = shutil.which("ollama")
        if executable is None and not self.ollama_injected:
            winget = shutil.which("winget")
            if winget:
                install = subprocess.run(
                    [
                        winget,
                        "install",
                        "--id",
                        "Ollama.Ollama",
                        "--exact",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                executable = shutil.which("ollama") if install.returncode == 0 else None
            if executable is None:
                return self.result("ollama", "blocked", "ollama_not_installed")

        try:
            models = self._run_async(self.ollama_client.list_models())
        except Exception as exc:
            return self.result("ollama", "blocked", f"ollama_unavailable:{type(exc).__name__}")
        matched = next((item for item in models if item.get("name") == self.settings.ollama_model), None)
        action = "skipped_existing"
        if matched is None:
            if self.ollama_injected or executable is None:
                return self.result("ollama", "blocked", "model_pull_requires_ollama_cli")
            pull = subprocess.run(
                [executable, "pull", self.settings.ollama_model],
                capture_output=True,
                text=True,
                check=False,
            )
            if pull.returncode != 0:
                return self.result("ollama", "failed", "ollama_pull_failed")
            try:
                models = self._run_async(self.ollama_client.list_models())
            except Exception as exc:
                return self.result("ollama", "failed", f"ollama_refresh_failed:{type(exc).__name__}")
            matched = next((item for item in models if item.get("name") == self.settings.ollama_model), None)
            action = "pulled"
        if matched is None:
            return self.result("ollama", "failed", "model_missing_after_pull")

        smoke = {"status": "skipped"}
        if hasattr(self.ollama_client, "chat"):
            try:
                result = self._run_async(
                    self.ollama_client.chat(LlmRequest([{"role": "user", "content": "Reply with OK."}], max_tokens=4))
                )
                smoke = {"status": "pass", "input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
            except Exception as exc:
                smoke = {"status": "fail", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                return self.result(
                    "ollama",
                    "failed",
                    "smoke_test_failed",
                    digest=matched.get("digest"),
                    bytes=matched.get("size"),
                    smoke_test=smoke,
                )
        manifest_path = self.paths.manifests / "ollama.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "model_id": "llm-default",
                    "provider": "ollama",
                    "physical_model": matched.get("name"),
                    "digest": matched.get("digest"),
                    "bytes": matched.get("size"),
                    "smoke_test": smoke,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return BootstrapResult(
            "ollama",
            "available",
            action,
            digest=matched.get("digest"),
            bytes=matched.get("size"),
            cache_path="Ollama managed local storage",
            manifest_path=str(manifest_path),
            smoke_test=smoke,
        )

    def ensure_chatterbox(self) -> BootstrapResult:
        try:
            provider = ChatterboxProvider(
                self.paths.huggingface_cache,
                python_executable=sys.executable,
                cache_only=False,
            )
            if not provider.dependency_available() or not provider.v3_api_available():
                install = self._install(list(self.PYTORCH_CUDA_PACKAGES), index_url=self.PYTORCH_CUDA_INDEX)
                if install.returncode != 0:
                    return self.result("chatterbox", "blocked", "cuda_torch_install_failed")
                install = self._install([self.CHATTERBOX_SOURCE])
                if install.returncode != 0:
                    return self.result("chatterbox", "blocked", "official_install_failed")
                importlib.invalidate_caches()
                provider = ChatterboxProvider(
                    self.paths.huggingface_cache,
                    python_executable=sys.executable,
                    cache_only=False,
                )
            if not provider.dependency_available():
                return self.result("chatterbox", "blocked", "dependency_missing_after_install")
            if not provider.v3_api_available():
                return self.result("chatterbox", "blocked", "v3_api_missing_after_install")
            if not self._cuda_torch_available():
                install = self._install(list(self.PYTORCH_CUDA_PACKAGES), index_url=self.PYTORCH_CUDA_INDEX)
                if install.returncode != 0 or not self._cuda_torch_available():
                    return self.result("chatterbox", "blocked", "cuda_runtime_unavailable")
            async def smoke_chatterbox():
                try:
                    return await provider.generate(TtsRequest("Hello from Chatterbox.", "en"))
                finally:
                    await provider.stop()

            result = self._run_async(smoke_chatterbox())
            files = self._cache_files(("models--ResembleAI--chatterbox",))
            manifest_path = self.paths.manifests / "chatterbox.json"
            manifest = ModelManifest(manifest_path).write(
                "tts-multilingual",
                "chatterbox",
                files,
                {"status": "pass", "audio_duration_ms": result.audio_duration_ms, "processing_ms": result.processing_ms},
                root=self.paths.huggingface_cache,
            )
            return BootstrapResult(
                "chatterbox",
                "available",
                "loaded_and_smoke_tested",
                bytes=manifest["bytes"],
                cache_path=str(self.paths.huggingface_cache),
                manifest_path=str(manifest_path),
                smoke_test=manifest["smoke_test"],
            )
        except Exception as exc:
            return self.result("chatterbox", "failed", f"{type(exc).__name__}:{str(exc)[:240]}")

    def ensure_vieneu(self) -> BootstrapResult:
        provider = None
        try:
            provider = VieneuProvider(
                self.paths.huggingface_cache,
                python_executable=sys.executable,
                cache_only=False,
            )
            if not provider.dependency_available():
                install = self._install(["vieneu"])
                if install.returncode != 0:
                    return self.result("vieneu", "blocked", "dependency_install_failed")
                importlib.invalidate_caches()
                provider = VieneuProvider(
                    self.paths.huggingface_cache,
                    python_executable=sys.executable,
                    cache_only=False,
                )
            if not provider.dependency_available():
                return self.result("vieneu", "blocked", "dependency_missing_after_install")
            async def smoke_vieneu():
                try:
                    return await provider.generate(TtsRequest("Xin chào.", "vi"))
                finally:
                    await provider.stop()

            result = self._run_async(smoke_vieneu())
            files = self._cache_files(
                (
                    "models--pnnbao-ump--VieNeu-TTS-v3-Turbo",
                    "models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano-ONNX",
                )
            )
            manifest_path = self.paths.manifests / "vieneu.json"
            manifest = ModelManifest(manifest_path).write(
                "tts-vietnamese",
                "vieneu",
                files,
                {"status": "pass", "audio_duration_ms": result.audio_duration_ms, "processing_ms": result.processing_ms},
                root=self.paths.huggingface_cache,
            )
            return BootstrapResult(
                "vieneu",
                "available",
                "loaded_and_smoke_tested",
                bytes=manifest["bytes"],
                cache_path=str(self.paths.huggingface_cache),
                manifest_path=str(manifest_path),
                smoke_test=manifest["smoke_test"],
            )
        except Exception as exc:
            return self.result("vieneu", "failed", f"{type(exc).__name__}:{str(exc)[:240]}")

    def _install(self, packages: list[str], index_url: str | None = None) -> subprocess.CompletedProcess:
        index_args = ["--index-url", index_url] if index_url else []
        uv = shutil.which("uv")
        if uv is not None:
            return subprocess.run(
                [uv, "pip", "install", "--python", sys.executable, *index_args, *packages],
                capture_output=True,
                text=True,
                check=False,
            )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *index_args,
                *packages,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _cuda_torch_available(self) -> bool:
        code = (
            "from pathlib import Path; import sys; "
            "site=Path(sys.executable).resolve().parents[1]/'Lib'/'site-packages'; "
            "sys.path[:]=[item for item in sys.path if item != str(site)]; sys.path.insert(0, str(site)); "
            "import torch; print(bool(torch.version.cuda and torch.cuda.is_available()))"
        )
        check = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        return check.returncode == 0 and check.stdout.strip().endswith("True")

    def _cache_files(self, repositories: tuple[str, ...]) -> list[Path]:
        files: list[Path] = []
        for repository in repositories:
            root = self.paths.huggingface_cache / repository
            if root.exists():
                files.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and not path.name.endswith((".lock", ".incomplete"))
                )
        return sorted(files)

    @staticmethod
    def _run_async(awaitable):
        return asyncio.run(awaitable)

    @staticmethod
    @contextlib.contextmanager
    def _exclusive_lock(path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and smoke-test configured local AI models")
    parser.add_argument("--report", default="data/manifests/bootstrap-report.json")
    args = parser.parse_args()
    settings = Settings()
    paths = AppPaths.from_settings(settings)
    report = BootstrapRunner(settings, paths).run_all()
    payload = {"models": [item.as_dict() for item in report]}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(item.status not in {"failed", "blocked"} for item in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
