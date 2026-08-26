from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "tts-server"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    lan_only: bool = False
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    hf_home: Path | None = None
    manifest_dir: Path | None = None
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_keep_alive: str = "0"
    max_gpu_ai_jobs: int = 1
    gpu_job_timeout_seconds: float = Field(default=900.0, gt=0)
    model_idle_sleep_seconds: float = Field(default=3600.0, gt=0)
    idle_reaper_interval_seconds: float = Field(default=60.0, gt=0)
    gpu_switch_unload: bool = True
    gpu_affinity_batch_size: int = Field(default=2, ge=1, le=16)
    max_gpu_queue: int = Field(default=16, ge=1, le=1024)
    max_cpu_queue: int = Field(default=16, ge=1, le=1024)
    max_concurrent_jobs_per_key: int = Field(default=2, ge=1, le=32)
    max_upload_bytes: int = Field(default=26_214_400, ge=1)
    max_reference_audio_seconds: int = Field(default=300, ge=1, le=86_400)
    job_retention_days: int = Field(default=30, ge=1)
    usage_retention_days: int = Field(default=90, ge=1)
    log_retention_days: int = Field(default=14, ge=1)
    default_rate_limit_per_minute: int = Field(default=60, ge=1)
    default_daily_quota_credits: int = Field(default=1000, ge=0)
    default_initial_credits: int = Field(default=1000, ge=0)
    llm_credits_per_1k_tokens: int = Field(default=1, ge=0)
    tts_credits_per_1k_chars: int = Field(default=1, ge=0)
    vieneu_cpu_only: bool = True
    vieneu_mode: str = "v3turbo"
    vieneu_device: str = "cpu"
    vieneu_backend: str = "onnx"
    vieneu_precision: str = "int8"
    vieneu_threads: int = Field(default=0, ge=0, le=256)
    vieneu_max_batch_size: int = Field(default=32, ge=1, le=256)

    @field_validator("max_gpu_ai_jobs")
    @classmethod
    def validate_gpu_job_limit(cls, value: int) -> int:
        if not 1 <= value <= 4:
            raise ValueError("MAX_GPU_AI_JOBS must be between 1 and 4")
        return value

    @field_validator("ollama_url")
    @classmethod
    def validate_local_ollama_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "127.0.0.1" or parsed.port != 11434:
            raise ValueError("OLLAMA_URL must point to local Ollama at 127.0.0.1:11434")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_public_bind_tls(self) -> "Settings":
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            if self.host != "0.0.0.0":
                try:
                    import ipaddress

                    address = ipaddress.ip_address(self.host)
                except ValueError as exc:
                    raise ValueError("HOST must be loopback, 0.0.0.0, or an RFC1918 private IPv4 address") from exc
                if address.version != 4 or not any(
                    address in ipaddress.ip_network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                ):
                    raise ValueError("HOST must be loopback, 0.0.0.0, or an RFC1918 private IPv4 address")
            if self.tls_cert_file is None or self.tls_key_file is None:
                if not self.lan_only:
                    raise ValueError("Insecure LAN bind requires LAN_ONLY=true")
            elif not self.tls_cert_file.is_file() or not self.tls_key_file.is_file():
                raise ValueError("TLS certificate and key files must exist for LAN/public bind")
        return self

    @model_validator(mode="after")
    def validate_vieneu_cpu_lane(self) -> "Settings":
        expected = {
            "vieneu_cpu_only": True,
            "vieneu_mode": "v3turbo",
            "vieneu_device": "cpu",
            "vieneu_backend": "onnx",
            "vieneu_precision": "int8",
        }
        invalid = [name for name, required in expected.items() if getattr(self, name) != required]
        if invalid:
            raise ValueError(f"VIENEU CPU lane requires fixed settings: {', '.join(invalid)}")
        return self
