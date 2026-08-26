import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class ModelManifest:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def write(
        self,
        model_id: str,
        provider: str,
        files: list[Path],
        smoke_result: dict,
        *,
        root: Path | str | None = None,
    ) -> dict:
        resolved_root = Path(root).expanduser().resolve() if root is not None else None
        file_entries = []
        total_bytes = 0
        for file_path in sorted((Path(item) for item in files), key=lambda item: str(item)):
            if not file_path.is_file():
                continue
            digest = hashlib.sha256()
            size = 0
            with file_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            total_bytes += size
            resolved_path = file_path.resolve()
            stored_path = str(resolved_path)
            if resolved_root is not None:
                try:
                    stored_path = resolved_path.relative_to(resolved_root).as_posix()
                except ValueError as exc:
                    raise ValueError(f"manifest file is outside root: {resolved_path}") from exc
            file_entries.append({"path": stored_path, "bytes": size, "sha256": digest.hexdigest()})

        payload = {
            "model_id": model_id,
            "provider": provider,
            "bytes": total_bytes,
            "files": file_entries,
            "smoke_test": smoke_result,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as target:
                target.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return payload

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))


def validate_manifest(
    manifest_path: Path | str,
    expected_model_id: str,
    expected_provider: str,
    *,
    cache_root: Path | str,
) -> tuple[bool, str | None, dict[str, int | str]]:
    """Validate manifest structure and every referenced cache file."""

    path = Path(manifest_path)
    root = Path(cache_root).expanduser().resolve()
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, "checkpoint_manifest_invalid", {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("model_id") != expected_model_id
        or payload.get("provider") != expected_provider
        or not isinstance(files, list)
        or not files
    ):
        return False, "checkpoint_manifest_invalid", {}

    total_bytes = 0
    for entry in files:
        if not isinstance(entry, dict):
            return False, "checkpoint_manifest_invalid", {}
        stored_path = entry.get("path")
        digest = entry.get("sha256")
        declared_bytes = entry.get("bytes")
        if (
            not isinstance(stored_path, str)
            or not stored_path
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(declared_bytes, int)
            or declared_bytes < 0
        ):
            return False, "checkpoint_manifest_invalid", {}
        try:
            int(digest, 16)
        except ValueError:
            return False, "checkpoint_manifest_invalid", {}
        candidate = Path(stored_path)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return False, "checkpoint_path_outside_cache", {"path": stored_path}
        if not resolved.is_file():
            return False, "checkpoint_file_missing", {"path": stored_path}
        if resolved.stat().st_size != declared_bytes:
            return False, "checkpoint_size_mismatch", {"path": stored_path}
        hasher = hashlib.sha256()
        with resolved.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            return False, "checkpoint_hash_mismatch", {"path": stored_path}
        total_bytes += declared_bytes

    return True, None, {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_bytes": len(raw),
        "manifest_file_count": len(files),
        "checkpoint_bytes": total_bytes,
    }
