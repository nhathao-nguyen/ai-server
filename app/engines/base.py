from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass(frozen=True)
class LlmRequest:
    messages: list[dict[str, Any]]
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    processing_ms: int = 0
    gpu_time_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmChunk:
    text: str = ""
    done: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    processing_ms: int = 0


@dataclass(frozen=True)
class TtsRequest:
    text: str
    language: str
    voice: str | None = None
    reference_audio_path: str | None = None
    reference_transcript: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    speed: float = 1.0


@dataclass(frozen=True)
class TtsResult:
    audio: bytes
    sample_rate: int
    audio_duration_ms: int
    processing_ms: int
    model: str
    voice: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderStatus:
    available: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class LlmProvider(Protocol):
    async def chat(self, request: LlmRequest) -> LlmResult: ...

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmChunk]: ...
