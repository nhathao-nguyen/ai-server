import math
import shutil
import subprocess
from pathlib import Path

from app.core.errors import ApiError


def probe_audio_duration_seconds(path: Path | str) -> float:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise ApiError("ffprobe_unavailable", "Audio validation runtime is unavailable", 503)
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiError("audio_probe_failed", "Reference audio could not be inspected", 422) from exc
    if result.returncode != 0:
        raise ApiError("invalid_reference_audio", "Reference audio is not a readable media file", 422)
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ApiError("invalid_reference_audio", "Reference audio duration is invalid", 422) from exc
    if not math.isfinite(duration) or duration < 0:
        raise ApiError("invalid_reference_audio", "Reference audio duration is invalid", 422)
    return duration
