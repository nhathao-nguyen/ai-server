from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    audio: Path
    cache: Path
    huggingface_cache: Path
    models: Path
    manifests: Path
    locks: Path
    logs: Path
    backups: Path
    temp: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> "AppPaths":
        root = Path(settings.data_dir).expanduser().resolve()
        configured_hf_home = settings.hf_home
        if configured_hf_home is None:
            huggingface_cache = root / "cache" / "huggingface"
        else:
            candidate = Path(configured_hf_home).expanduser()
            # Relative HF_HOME values are scoped to DATA_DIR so temporary/test
            # runtimes cannot accidentally reuse the project's live cache.
            huggingface_cache = candidate if candidate.is_absolute() else root / candidate
        configured_manifest_dir = settings.manifest_dir
        if configured_manifest_dir is None:
            manifest_dir = root / "manifests"
        else:
            candidate = Path(configured_manifest_dir).expanduser()
            manifest_dir = candidate if candidate.is_absolute() else root / candidate

        return cls(
            root=root,
            database=root / "server.db",
            audio=root / "audio",
            cache=root / "cache",
            huggingface_cache=huggingface_cache,
            models=root / "models",
            manifests=manifest_dir.resolve(),
            locks=root / "locks",
            logs=root / "logs",
            backups=root / "backups",
            temp=root / "temp",
        )

    def ensure_directories(self) -> None:
        for path in (
            self.root,
            self.audio,
            self.cache,
            self.huggingface_cache,
            self.models,
            self.locks,
            self.logs,
            self.backups,
            self.temp,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if self.manifests == (self.root / "manifests").resolve():
            self.manifests.mkdir(parents=True, exist_ok=True)
