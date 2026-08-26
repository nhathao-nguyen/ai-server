from pathlib import Path

import app.core.model_manifest as model_manifest


def test_manifest_rejects_missing_or_tampered_file(tmp_path: Path) -> None:
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"original")
    manifest_path = tmp_path / "manifest.json"
    model_manifest.ModelManifest(manifest_path).write(
        "tts-vietnamese", "vieneu", [model_file], {"status": "pass"}
    )
    model_file.write_bytes(b"tampered")
    assert hasattr(model_manifest, "validate_manifest")
    valid, reason, _details = model_manifest.validate_manifest(
        manifest_path, "tts-vietnamese", "vieneu", cache_root=tmp_path
    )
    assert not valid
    assert reason == "checkpoint_hash_mismatch"


def test_manifest_accepts_matching_file(tmp_path: Path) -> None:
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"valid")
    manifest_path = tmp_path / "manifest.json"
    model_manifest.ModelManifest(manifest_path).write(
        "tts-vietnamese", "vieneu", [model_file], {"status": "pass"}
    )
    assert hasattr(model_manifest, "validate_manifest")
    valid, reason, details = model_manifest.validate_manifest(
        manifest_path, "tts-vietnamese", "vieneu", cache_root=tmp_path
    )
    assert valid
    assert reason is None
    assert details["manifest_file_count"] == 1
