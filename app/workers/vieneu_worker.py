import contextlib
import io
import json
import os
import sys
import wave
from pathlib import Path

from app.engines.tts_options import VIENEU_DEFAULT_VOICE, normalize_tts_options
from app.workers.protocol import safe_worker_error


def audio_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        frames = source.getnframes()
    return round(frames * 1000 / rate) if rate else 0


def prefer_virtualenv_site() -> None:
    site_packages = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages"
    site_package_text = str(site_packages)
    if site_packages.is_dir():
        sys.path[:] = [entry for entry in sys.path if entry != site_package_text]
        sys.path.insert(0, site_package_text)


DEFAULT_MODEL_CONFIG = {
    "mode": "v3turbo",
    "device": "cpu",
    "backend": "onnx",
    "precision": "int8",
    "threads": 0,
    "max_batch_size": 32,
}


def load_model(config: dict | None = None):
    runtime_config = {**DEFAULT_MODEL_CONFIG, **(config or {})}
    prefer_virtualenv_site()
    with contextlib.redirect_stdout(io.StringIO()):
        from vieneu import Vieneu

        return Vieneu(
            mode=runtime_config["mode"],
            device=runtime_config["device"],
            backend=runtime_config["backend"],
            precision=runtime_config["precision"],
            threads=int(runtime_config["threads"]),
            max_batch_size=int(runtime_config["max_batch_size"]),
        )


def _runtime_config() -> dict:
    raw = os.environ.get("TTS_VIENEU_CONFIG", "")
    if not raw:
        return dict(DEFAULT_MODEL_CONFIG)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_MODEL_CONFIG)
    return {**DEFAULT_MODEL_CONFIG, **value} if isinstance(value, dict) else dict(DEFAULT_MODEL_CONFIG)


def generate_one(tts, request: dict) -> dict:
    raw_options = request.get("options")
    options = normalize_tts_options("vieneu", {} if raw_options is None else raw_options)
    infer_options = {}
    reference_audio_path = request.get("reference_audio_path")
    if reference_audio_path:
        infer_options["ref_audio"] = reference_audio_path
    else:
        infer_options["voice"] = request.get("voice") or VIENEU_DEFAULT_VOICE
    infer_options.update(
        {
            "denoise": options["denoise"],
            "use_ref_codes": options["use_ref_codes"],
            "temperature": options["temperature"],
            "top_k": options["top_k"],
            "top_p": options["top_p"],
            "max_new_frames": options["max_new_frames"],
            "repetition_penalty": options["repetition_penalty"],
            "repetition_window": options["repetition_window"],
            "max_chars": options["max_chars"],
            "silence_p": options["silence_p"],
            "crossfade_p": options["crossfade_p"],
            "apply_watermark": options["apply_watermark"],
            "batch_size": options["batch_size"],
        }
    )
    output_path = Path(request["output_path"])
    with contextlib.redirect_stdout(io.StringIO()):
        audio = tts.infer(request["text"], **infer_options)
        tts.save(audio, str(output_path))
    return {
        "ok": True,
        "sample_rate": int(getattr(tts, "sample_rate", 48_000)),
        "audio_duration_ms": audio_duration_ms(output_path),
        "effective_options": options,
        "voice": "reference" if reference_audio_path else request.get("voice") or VIENEU_DEFAULT_VOICE,
        "reference_transcript_ignored": bool(request.get("reference_transcript")),
    }


def main() -> int:
    try:
        # VieNeu and its Hugging Face dependencies emit progress/log text to
        # stdout.  stdout is the JSONL control channel, so keep those messages
        # away from protocol frames.
        tts = load_model(_runtime_config())
        print(json.dumps({"ready": True}), flush=True)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": safe_worker_error(exc)}), flush=True)
        return 1

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            print(json.dumps(generate_one(tts, request)), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": safe_worker_error(exc)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
