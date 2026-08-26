from dataclasses import dataclass

from app.core.errors import ApiError


@dataclass(frozen=True)
class EngineRoute:
    provider: str
    logical_model: str
    physical_model: str | None
    requires_gpu: bool


class EngineRouter:
    CHATTERBOX_LANGUAGES = frozenset(
        {
            "ar",
            "da",
            "de",
            "el",
            "en",
            "es",
            "fi",
            "fr",
            "he",
            "hi",
            "it",
            "ja",
            "ko",
            "ms",
            "nl",
            "no",
            "pl",
            "pt",
            "ru",
            "sv",
            "sw",
            "tr",
            "zh",
        }
    )

    def __init__(self, ollama_model: str = "qwen3.5:9b") -> None:
        self.ollama_model = ollama_model

    def resolve_llm(self, logical_model: str | None) -> EngineRoute:
        if logical_model not in {None, "llm-default", self.ollama_model}:
            raise ApiError("unknown_model", "Unknown LLM model", 400, {"model": logical_model})
        return EngineRoute("ollama", "llm-default", self.ollama_model, True)

    def resolve_tts(self, language: str, requested_model: str | None) -> EngineRoute:
        normalized = language.lower().split("-")[0].strip()
        if requested_model not in {None, "tts-multilingual", "tts-vietnamese"}:
            raise ApiError("unknown_model", "Unknown TTS model", 400, {"model": requested_model})
        if requested_model == "tts-vietnamese" or normalized in {"vi", "vie"}:
            if normalized not in {"vi", "vie"}:
                raise ApiError("language_model_mismatch", "VieNeu only handles Vietnamese", 400)
            return EngineRoute("vieneu", "tts-vietnamese", None, False)
        if normalized not in self.CHATTERBOX_LANGUAGES:
            raise ApiError("unsupported_language", "The requested language is not supported", 422, {"language": language})
        return EngineRoute("chatterbox", "tts-multilingual", None, True)
