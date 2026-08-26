import argparse
import contextlib
import io
import json
import sys
import struct
import wave
from pathlib import Path

from app.engines.tts_options import normalize_tts_options
from app.workers.protocol import safe_worker_error


def audio_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as source:
            rate = source.getframerate()
            frames = source.getnframes()
        return round(frames * 1000 / rate) if rate else 0
    except wave.Error:
        return _riff_duration_ms(path)


def _riff_duration_ms(path: Path) -> int:
    """Read PCM and IEEE-float WAV headers without decoding audio samples."""
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise wave.Error("unsupported audio container")
        sample_rate = channels = bits_per_sample = data_size = None
        while True:
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            chunk = source.read(chunk_size)
            if chunk_size % 2:
                source.seek(1, 1)
            if chunk_id == b"fmt " and len(chunk) >= 16:
                audio_format, channels, sample_rate, _, _, bits_per_sample = struct.unpack("<HHIIHH", chunk[:16])
                if audio_format not in (1, 3):
                    raise wave.Error(f"unsupported WAV format: {audio_format}")
            elif chunk_id == b"data":
                data_size = chunk_size
            if sample_rate and channels and bits_per_sample and data_size is not None:
                bytes_per_frame = channels * bits_per_sample // 8
                frames = data_size // bytes_per_frame if bytes_per_frame else 0
                return round(frames * 1000 / sample_rate) if sample_rate else 0
    raise wave.Error("WAV header is incomplete")


def prefer_virtualenv_site() -> None:
    site_packages = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages"
    site_package_text = str(site_packages)
    if site_packages.is_dir():
        # The base Windows Python can put its global site-packages ahead of the
        # virtualenv. Keep torch and torchaudio from the same environment.
        sys.path[:] = [entry for entry in sys.path if entry != site_package_text]
        sys.path.insert(0, site_package_text)


def load_model():
    prefer_virtualenv_site()
    with contextlib.redirect_stdout(io.StringIO()):
        import torchaudio as ta
        import perth
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        # VieNeu depends on the small ``perth`` thread-local helper package,
        # while Chatterbox expects Resemble Perth's watermark implementation.
        # Both distributions install the same top-level module and the helper
        # package can overwrite only ``__init__.py``. Resolve the actual
        # watermark class from its bundled submodule when it is not re-exported.
        if not hasattr(perth, "PerthImplicitWatermarker"):
            from perth.perth_net.perth_net_implicit.perth_watermarker import (
                PerthImplicitWatermarker,
            )

            perth.PerthImplicitWatermarker = PerthImplicitWatermarker

        model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
    return ta, model


def generate_one(ta, model, request: dict) -> dict:
    raw_options = request.get("options")
    options = normalize_tts_options("chatterbox", {} if raw_options is None else raw_options)
    generate_options = {
        "exaggeration": options["exaggeration"],
        "cfg_weight": options["cfg_weight"],
        "temperature": options["temperature"],
        "repetition_penalty": options["repetition_penalty"],
        "min_p": options["min_p"],
        "top_p": options["top_p"],
    }
    if request.get("reference_audio_path"):
        generate_options["audio_prompt_path"] = request["reference_audio_path"]
    with contextlib.redirect_stdout(io.StringIO()):
        waveform = model.generate(
            request["text"],
            language_id=request.get("language") or "en",
            **generate_options,
        )
        output_path = Path(request["output_path"])
        ta.save(str(output_path), waveform, model.sr)
    return {
        "ok": True,
        "sample_rate": int(model.sr),
        "audio_duration_ms": audio_duration_ms(output_path),
        "effective_options": options,
        "voice": "reference" if request.get("reference_audio_path") else "default",
        "reference_transcript_ignored": bool(request.get("reference_transcript")),
    }


def persistent_main() -> int:
    try:
        ta, model = load_model()
        print(json.dumps({"ready": True}), flush=True)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": safe_worker_error(exc)}), flush=True)
        return 1

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if request.get("shutdown"):
                return 0
            print(json.dumps(generate_one(ta, model, request)), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": safe_worker_error(exc)}), flush=True)
            return 1
    return 0


def single_request_main(request_path: str, output_path: str) -> int:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    request["output_path"] = output_path
    try:
        ta, model = load_model()
        print(json.dumps(generate_one(ta, model, request)), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": safe_worker_error(exc)}), flush=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persistent", action="store_true")
    parser.add_argument("--request-file")
    parser.add_argument("--output-file")
    args = parser.parse_args()
    if args.persistent:
        return persistent_main()
    if not args.request_file or not args.output_file:
        parser.error("--request-file and --output-file are required without --persistent")
    return single_request_main(args.request_file, args.output_file)


if __name__ == "__main__":
    raise SystemExit(main())
