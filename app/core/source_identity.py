import hashlib
import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXCLUDED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".venv",
    ".venv.previous",
    "__pycache__",
    "audio",
    "backups",
    "build",
    "cache",
    "data",
    "docs",
    "dist",
    "logs",
    "models",
    "node_modules",
    "src-tauri/target",
    "target",
    "temp",
}
EXCLUDED_FILES = {".coverage", ".env"}


def _is_excluded(relative_path: Path) -> bool:
    normalized = relative_path.as_posix()
    if normalized.startswith("src-tauri/gen/"):
        return True
    return any(part in EXCLUDED_DIRECTORIES for part in relative_path.parts) or relative_path.name in EXCLUDED_FILES


def source_identity(root: Path) -> str:
    """Return a stable digest of source inputs, independent of absolute paths and mtimes."""

    root = root.resolve()
    entries: list[tuple[str, str]] = []
    for directory, directories, filenames in os.walk(root):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        directories[:] = [
            name
            for name in directories
            if not _is_excluded(relative_directory / name)
        ]
        for filename in filenames:
            path = directory_path / filename
            relative = path.relative_to(root)
            if _is_excluded(relative) or path.suffix == ".pyc":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative.as_posix(), digest))
    entries.sort()
    hasher = hashlib.sha256()
    for relative, digest in entries:
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _read_version(path: Path, *, toml: bool = False) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if toml else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    return str(data.get("project", {}).get("version")) if toml else str(data.get("version"))


def _read_json_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("version") if isinstance(data, dict) else None
    return str(value) if value is not None else None


def dependency_lock_digests(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in ("uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "src-tauri/Cargo.lock"):
        path = root / relative
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def manifest_digests(manifest_dir: Path) -> dict[str, str]:
    if not manifest_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted(manifest_dir.glob("*.json")):
        if path.is_file():
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def build_info(root: Path, *, manifest_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    python_version = _read_version(root / "pyproject.toml", toml=True)
    frontend_version = _read_json_version(root / "package.json")
    tauri_version = _read_json_version(root / "src-tauri" / "tauri.conf.json")
    versions = {value for value in (python_version, frontend_version, tauri_version) if value}
    if len(versions) > 1:
        raise ValueError("Python, frontend, and Tauri versions are not synchronized")
    return {
        "version": python_version,
        "component_versions": {
            "python": python_version,
            "frontend": frontend_version,
            "tauri": tauri_version,
        },
        "source_identity": source_identity(root),
        "source_identity_type": "source_tree_sha256",
        "source_dirty": False,
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "dependency_lock_digests": dependency_lock_digests(root),
        "manifest_digests": manifest_digests(manifest_dir) if manifest_dir else {},
    }
