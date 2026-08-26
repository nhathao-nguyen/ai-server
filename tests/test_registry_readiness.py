from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.model_registry import ModelRegistry
from app.core.paths import AppPaths
from app.engines.base import ProviderStatus


class _Probe:
    runtime_state = "available"

    def __init__(self) -> None:
        self.deep_calls = 0

    def dependency_available(self) -> bool:
        return True

    def status(self) -> ProviderStatus:
        return ProviderStatus(True)

    async def capability_status(self) -> ProviderStatus:
        self.deep_calls += 1
        return ProviderStatus(True, details={"worker_probe": "pass"})


class _Ollama:
    async def list_models(self):
        return [{"name": "qwen3.5:9b"}]


@pytest.mark.asyncio
async def test_refresh_is_non_invasive_by_default(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    paths = AppPaths.from_settings(settings)
    paths.ensure_directories()
    (paths.manifests / "chatterbox.json").write_text("{}", encoding="utf-8")
    (paths.manifests / "vieneu.json").write_text("{}", encoding="utf-8")
    probe = _Probe()
    registry = ModelRegistry(
        settings,
        paths,
        ollama_probe=_Ollama(),
        providers={"chatterbox": probe, "vieneu": probe},
    )
    statuses = await registry.refresh()
    assert len(statuses) == 3
    assert probe.deep_calls == 0
