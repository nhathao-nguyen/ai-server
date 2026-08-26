from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=100_000)


class ChatCompletionRequest(BaseModel):
    model: str = "llm-default"
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32_768)
    user: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")


class TranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    target_language: str = Field(min_length=2, max_length=32)
    source_language: str | None = Field(default=None, max_length=32)
    style: str | None = Field(default=None, max_length=128)
    model_config = ConfigDict(extra="forbid")


class SpeechRequest(BaseModel):
    model: str | None = None
    input: str | None = Field(default=None, max_length=20_000)
    text: str | None = Field(default=None, max_length=20_000)
    language: str = Field(default="en", min_length=2, max_length=32)
    voice: str | None = Field(default=None, max_length=128)
    response_format: str = Field(default="wav", pattern="^wav$")
    speed: float = Field(default=1.0, gt=0.1, le=4.0)
    options: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_input(self) -> "SpeechRequest":
        if not (self.input or self.text or "").strip():
            raise ValueError("input is required")
        return self

    @property
    def text_value(self) -> str:
        return (self.input or self.text or "").strip()


class PreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    language: str = Field(default="en", min_length=2, max_length=32)
    model: str | None = None
    voice: str | None = Field(default=None, max_length=128)
    speed: float = Field(default=1.0, gt=0.1, le=4.0)
    options: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class ApiKeyCreateRequest(BaseModel):
    scopes: set[str] = Field(min_length=1)
    label: str = Field(default="", max_length=128)
    owner_note: str = Field(default="", max_length=500)
    expires_at: str | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1)
    daily_quota_credits: int | None = Field(default=None, ge=0)
    initial_credits: int | None = Field(default=None, ge=0)
