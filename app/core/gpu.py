import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuStatus:
    available: bool
    name: str | None = None
    total_memory_mb: int | None = None
    used_memory_mb: int | None = None
    free_memory_mb: int | None = None
    utilization_percent: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "name": self.name,
            "total_memory_mb": self.total_memory_mb,
            "used_memory_mb": self.used_memory_mb,
            "free_memory_mb": self.free_memory_mb,
            "utilization_percent": self.utilization_percent,
            "reason": self.reason,
        }


class GpuStatusProbe:
    def read(self) -> GpuStatus:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return GpuStatus(False, reason="nvidia_smi_missing")
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return GpuStatus(False, reason=f"nvidia_smi_error:{type(exc).__name__}")
        if result.returncode != 0 or not result.stdout.strip():
            return GpuStatus(False, reason="nvidia_smi_failed")
        try:
            name, total, used, free, utilization = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
            return GpuStatus(True, name, int(total), int(used), int(free), int(utilization))
        except (ValueError, IndexError):
            return GpuStatus(False, reason="nvidia_smi_parse_failed")
