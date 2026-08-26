import hashlib
import inspect
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.core.config import Settings
from app.core.paths import AppPaths
from app.engines.tts_options import provider_capabilities


LOCAL_PHYSICAL_MODELS = {
    "tts-multilingual": "Chatterbox Multilingual V3",
    "tts-vietnamese": "VieNeu v3 Turbo CPU ONNX/int8",
}


@dataclass(frozen=True)
class ModelStatus:
    id: str
    provider: str
    physical_model: str | None
    available: bool
    reason: str | None = None
    cache_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    state: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "available" if self.available else "unavailable"

    def as_dict(self) -> dict[str, Any]:
        state = self.state or ("available" if self.available else "unavailable")
        payload = {
            "id": self.id,
            "provider": self.provider,
            "physical_model": self.physical_model,
            "available": self.available,
            "status": self.status,
            "state": state,
            "lifecycle_state": state,
            "reason": self.reason,
            "cache_path": self.cache_path,
            "details": self.details,
            "capabilities": self.capabilities,
        }
        for key, value in self.capabilities.items():
            payload.setdefault(key, value)
        return payload


class ModelRegistry:
    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        ollama_probe=None,
        providers: Mapping[str, Any] | None = None,
        capability_probe: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.ollama_probe = ollama_probe
        self.providers = dict(providers or {})
        self.capability_probe = capability_probe
        self._statuses: list[ModelStatus] = []

    async def refresh(self, *, deep_probe: bool = False) -> list[ModelStatus]:
        statuses = [await self._ollama_status()]
        statuses.append(
            await self._local_status("tts-multilingual", "chatterbox", "chatterbox", deep_probe=deep_probe)
        )
        statuses.append(
            await self._local_status("tts-vietnamese", "vieneu", "vieneu", deep_probe=deep_probe)
        )
        self._statuses = statuses
        return list(statuses)

    def current(self) -> list[ModelStatus]:
        return list(self._statuses)

    async def _ollama_status(self) -> ModelStatus:
        base = {
            "id": "llm-default",
            "provider": "ollama",
            "physical_model": self.settings.ollama_model,
            "cache_path": None,
        }
        if self.ollama_probe is None:
            return ModelStatus(**base, available=False, reason="ollama_probe_missing", state="unavailable")
        try:
            models = await self.ollama_probe.list_models()
        except Exception as exc:
            return ModelStatus(
                **base,
                available=False,
                reason="ollama_unavailable",
                details={"error": str(exc)[:200]},
                state="unavailable",
            )
        matched = next((item for item in models if self._model_name(item) == self.settings.ollama_model), None)
        if matched is None:
            return ModelStatus(**base, available=False, reason="model_missing", state="unavailable")
        return ModelStatus(
            **base,
            available=True,
            state=self._runtime_state("ollama", "available"),
            details={
                "digest": self._model_value(matched, "digest"),
                "size": self._model_value(matched, "size"),
            },
        )

    async def _local_status(
        self,
        model_id: str,
        provider: str,
        module_name: str,
        *,
        deep_probe: bool = False,
    ) -> ModelStatus:
        manifest = self.paths.manifests / f"{provider}.json"
        physical_model = LOCAL_PHYSICAL_MODELS[model_id]
        capabilities = provider_capabilities(provider)
        provider_instance = self.providers.get(provider)
        if provider == "vieneu":
            configured_runtime = getattr(provider_instance, "runtime_config", None)
            capabilities["runtime"] = (
                dict(configured_runtime)
                if isinstance(configured_runtime, Mapping)
                else {
                    "mode": self.settings.vieneu_mode,
                    "device": self.settings.vieneu_device,
                    "backend": self.settings.vieneu_backend,
                    "precision": self.settings.vieneu_precision,
                    "threads": self.settings.vieneu_threads,
                    "max_batch_size": self.settings.vieneu_max_batch_size,
                }
            )
        dependency_checker = getattr(provider_instance, "dependency_available", None)
        try:
            dependency_present = (
                bool(dependency_checker())
                if callable(dependency_checker)
                else importlib.util.find_spec(module_name) is not None
            )
        except (ImportError, OSError, RuntimeError):
            dependency_present = False
        if not dependency_present:
            return ModelStatus(
                id=model_id,
                provider=provider,
                physical_model=physical_model,
                available=False,
                reason="dependency_missing",
                cache_path=str(self.paths.huggingface_cache),
                details={"manifest": str(manifest)},
                state="unavailable",
                capabilities=capabilities,
            )
        if not manifest.exists():
            return ModelStatus(
                id=model_id,
                provider=provider,
                physical_model=physical_model,
                available=False,
                reason="checkpoint_missing",
                cache_path=str(self.paths.huggingface_cache),
                details={"manifest": str(manifest)},
                state="unavailable",
                capabilities=capabilities,
            )
        provider_status = getattr(provider_instance, "status", None)
        capability_details: dict[str, Any] = {}
        status_details: dict[str, Any] = {}
        if callable(provider_status):
            checked = provider_status()
            if not checked.available:
                return ModelStatus(
                    id=model_id,
                    provider=provider,
                    physical_model=physical_model,
                    available=False,
                    reason=checked.reason,
                    cache_path=str(self.paths.huggingface_cache),
                    details={**self._manifest_details(manifest), **checked.details},
                    state="unavailable",
                    capabilities=capabilities,
                )
            status_details = dict(checked.details)
            provider_capability_probe = (
                self.capability_probe
                if self.capability_probe is not None and provider == "chatterbox"
                else getattr(provider_instance, "capability_status", None)
            )
            if deep_probe and callable(provider_capability_probe):
                capability_checked = (
                    provider_capability_probe(provider)
                    if self.capability_probe is not None and provider == "chatterbox"
                    else provider_capability_probe()
                )
                if inspect.isawaitable(capability_checked):
                    capability_checked = await capability_checked
                if not capability_checked.available:
                    return ModelStatus(
                        id=model_id,
                        provider=provider,
                        physical_model=physical_model,
                        available=False,
                        reason=capability_checked.reason,
                        cache_path=str(self.paths.huggingface_cache),
                        details={
                            **self._manifest_details(manifest),
                            **capability_checked.details,
                        },
                        state="unavailable",
                        capabilities=capabilities,
                    )
                capability_details = dict(capability_checked.details)
        return ModelStatus(
            id=model_id,
            provider=provider,
            physical_model=physical_model,
            available=True,
            cache_path=str(self.paths.huggingface_cache),
            details={**self._manifest_details(manifest), **status_details, **capability_details},
            state=self._runtime_state(provider, "available"),
            capabilities=capabilities,
        )

    def _runtime_state(self, provider: str, fallback: str) -> str:
        instance = self.providers.get(provider)
        state = getattr(instance, "runtime_state", None)
        if callable(state):
            state = state()
        return str(state or fallback)

    @staticmethod
    def _manifest_details(manifest: Path) -> dict[str, Any]:
        try:
            content = manifest.read_bytes()
            payload = json.loads(content.decode("utf-8"))
            files = payload.get("files", []) if isinstance(payload, dict) else []
            return {
                "manifest": str(manifest),
                "manifest_sha256": hashlib.sha256(content).hexdigest(),
                "manifest_bytes": len(content),
                "manifest_file_count": len(files) if isinstance(files, list) else 0,
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"manifest": str(manifest), "manifest_error": "invalid_manifest"}

    @staticmethod
    def _model_name(item: Any) -> str | None:
        return item.get("name") if isinstance(item, dict) else getattr(item, "name", None)

    @staticmethod
    def _model_value(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)
