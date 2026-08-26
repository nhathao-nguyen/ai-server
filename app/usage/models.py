from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class UsageEventInput:
    api_key_id: str
    endpoint: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    characters: int = 0
    audio_duration_ms: int = 0
    processing_ms: int = 0
    gpu_time_ms: int | None = None
    voice: str | None = None
    status: str = "succeeded"
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class UsageEvent:
    id: int
    api_key_id: str
    endpoint: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    characters: int
    audio_duration_ms: int
    processing_ms: int
    gpu_time_ms: int | None
    voice: str | None
    credits_charged: int
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class CreditReservation:
    id: str
    api_key_id: str
    amount: int
    request_id: str | None = None
    state: str = "reserved"
