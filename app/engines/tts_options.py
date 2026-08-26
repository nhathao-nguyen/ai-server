from __future__ import annotations

import math
from typing import Any

from app.core.errors import ApiError
from app.engines.base import TtsRequest


CHATTERBOX_SUPPORTED_LANGUAGES = (
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
)

CHATTERBOX_DEFAULT_OPTIONS = {
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "repetition_penalty": 1.2,
    "min_p": 0.05,
    "top_p": 1.0,
}
CHATTERBOX_OPTION_NAMES = frozenset(CHATTERBOX_DEFAULT_OPTIONS)

VIENEU_DEFAULT_OPTIONS = {
    "denoise": True,
    "use_ref_codes": True,
    "temperature": 0.8,
    "top_k": 25,
    "top_p": 0.95,
    "max_new_frames": 300,
    "repetition_penalty": 1.2,
    "repetition_window": 64,
    "max_chars": 256,
    "silence_p": 0.15,
    "crossfade_p": 0.0,
    "apply_watermark": True,
    "batch_size": None,
}
VIENEU_OPTION_NAMES = frozenset(VIENEU_DEFAULT_OPTIONS)
VIENEU_COMPATIBILITY_OPTIONS = frozenset({"style"})
VIENEU_PRESET_VOICES = (
    "Minh Đức",
    "Phạm Tuyên",
    "Thái Sơn",
    "Xuân Vĩnh",
    "Thanh Bình",
    "Trúc Ly",
    "Ngọc Linh",
    "Đoan Trang",
    "Mai Anh",
    "Thục Đoan",
    "Minh Triết",
    "Thùy Dung",
    "Quang Sơn",
    "Ngọc Trân",
    "Mỹ Duyên",
    "Quỳnh Anh",
    "Đức Trí",
    "Kim Thanh",
    "Ngọc Huyền",
    "Adam",
)
VIENEU_DEFAULT_VOICE = "Adam"


def _invalid_option(name: str, message: str) -> ApiError:
    return ApiError("invalid_tts_option", f"Invalid TTS option '{name}': {message}", 422, {"option": name})


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _invalid_option(name, "must be a finite number")
    return float(value)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_option(name, "must be an integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise _invalid_option(name, "must be a boolean")
    return value


def _bounded_number(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None, exclusive_minimum: bool = False) -> float:
    number = _number(value, name)
    if minimum is not None and (number <= minimum if exclusive_minimum else number < minimum):
        operator = ">" if exclusive_minimum else ">="
        raise _invalid_option(name, f"must be {operator} {minimum}")
    if maximum is not None and number > maximum:
        raise _invalid_option(name, f"must be <= {maximum}")
    return number


def _validate_chatterbox(name: str, value: Any) -> Any:
    if name in {"exaggeration", "cfg_weight"}:
        return _bounded_number(value, name, minimum=0.0, maximum=2.0)
    if name == "temperature":
        return _bounded_number(value, name, minimum=0.0, maximum=2.0, exclusive_minimum=True)
    if name == "repetition_penalty":
        return _bounded_number(value, name, minimum=0.1, maximum=5.0)
    if name in {"min_p", "top_p"}:
        return _bounded_number(value, name, minimum=0.0, maximum=1.0, exclusive_minimum=name == "top_p")
    raise _invalid_option(name, "is not supported by Chatterbox")


def _validate_vieneu(name: str, value: Any) -> Any:
    if name in {"denoise", "use_ref_codes", "apply_watermark"}:
        return _boolean(value, name)
    if name == "temperature":
        return _bounded_number(value, name, minimum=0.0, maximum=2.0, exclusive_minimum=True)
    if name == "top_k":
        number = _integer(value, name)
        if not 1 <= number <= 1000:
            raise _invalid_option(name, "must be between 1 and 1000")
        return number
    if name == "top_p":
        return _bounded_number(value, name, minimum=0.0, maximum=1.0, exclusive_minimum=True)
    if name in {"max_new_frames", "repetition_window", "max_chars"}:
        number = _integer(value, name)
        maximum = 10_000 if name == "max_chars" else 4096
        if not 1 <= number <= maximum:
            raise _invalid_option(name, f"must be between 1 and {maximum}")
        return number
    if name == "repetition_penalty":
        return _bounded_number(value, name, minimum=0.1, maximum=5.0)
    if name in {"silence_p", "crossfade_p"}:
        return _bounded_number(value, name, minimum=0.0, maximum=1.0)
    if name == "batch_size":
        if value is None:
            return None
        number = _integer(value, name)
        if not 1 <= number <= 256:
            raise _invalid_option(name, "must be between 1 and 256 or null")
        return number
    raise _invalid_option(name, "is not supported by VieNeu")


def normalize_tts_options(provider: str, options: dict[str, Any] | None) -> dict[str, Any]:
    supplied = {} if options is None else options
    if not isinstance(supplied, dict):
        raise ApiError("invalid_tts_option", "TTS options must be a JSON object", 422)
    if provider == "chatterbox":
        defaults = CHATTERBOX_DEFAULT_OPTIONS
        allowed = CHATTERBOX_OPTION_NAMES
        validator = _validate_chatterbox
    elif provider == "vieneu":
        defaults = VIENEU_DEFAULT_OPTIONS
        allowed = VIENEU_OPTION_NAMES | VIENEU_COMPATIBILITY_OPTIONS
        validator = _validate_vieneu
    else:
        raise ApiError("unknown_tts_provider", f"Unknown TTS provider '{provider}'", 422, {"provider": provider})

    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise ApiError(
            "unknown_tts_option",
            f"Unsupported TTS option(s) for {provider}: {', '.join(unknown)}",
            422,
            {"provider": provider, "options": unknown},
        )
    effective = dict(defaults)
    for name, value in supplied.items():
        if provider == "vieneu" and name == "style":
            if value is not None and not isinstance(value, str):
                raise _invalid_option(name, "must be a string or null")
            continue
        effective[name] = validator(name, value)
    return effective


def validate_tts_request(provider: str, request: TtsRequest) -> dict[str, Any]:
    if not isinstance(request.speed, (int, float)) or isinstance(request.speed, bool) or not math.isfinite(float(request.speed)):
        raise ApiError("invalid_speed", "speed must be a finite number", 422)
    if request.speed != 1.0:
        raise ApiError(
            "unsupported_speed",
            "Only speed=1.0 is supported until pitch-preserving time-stretch is implemented",
            422,
            {"speed": request.speed},
        )
    if provider == "chatterbox":
        if request.voice not in {None, "default"}:
            raise ApiError(
                "named_voice_unsupported",
                "Chatterbox has no named voice catalog; use voice='default' or omit voice",
                422,
                {"voice": request.voice},
            )
    elif provider == "vieneu" and request.voice is not None and request.voice not in VIENEU_PRESET_VOICES:
        raise ApiError("unknown_voice", f"VieNeu preset voice '{request.voice}' was not found", 422, {"voice": request.voice})
    return normalize_tts_options(provider, request.options)


def effective_voice(provider: str, request: TtsRequest) -> str:
    if request.reference_audio_path:
        return "reference"
    if provider == "vieneu":
        return request.voice or VIENEU_DEFAULT_VOICE
    return "default"


def provider_capabilities(provider: str) -> dict[str, Any]:
    if provider == "chatterbox":
        return {
            "supported_languages": list(CHATTERBOX_SUPPORTED_LANGUAGES),
            "supported_options": sorted(CHATTERBOX_OPTION_NAMES),
            "default_options": dict(CHATTERBOX_DEFAULT_OPTIONS),
            "voice_mode": "built_in_conditioning_or_reference",
            "preset_voice_count": 0,
            "preset_voice_names": [],
            "default_voice": "default",
            "supports_voice_clone": True,
            "supports_named_voice": False,
            "ignored_options": [],
            "ignored_features": ["reference_transcript"],
            "unsupported_features": ["happy", "sad", "angry", "named voice catalog", "speed != 1.0"],
        }
    if provider == "vieneu":
        return {
            "supported_languages": ["vi", "vie"],
            "supported_options": sorted(VIENEU_OPTION_NAMES),
            "default_options": dict(VIENEU_DEFAULT_OPTIONS),
            "voice_mode": "preset_or_reference",
            "preset_voice_count": len(VIENEU_PRESET_VOICES),
            "preset_voice_names": list(VIENEU_PRESET_VOICES),
            "default_voice": VIENEU_DEFAULT_VOICE,
            "supports_voice_clone": True,
            "supports_named_voice": True,
            "ignored_options": sorted(VIENEU_COMPATIBILITY_OPTIONS),
            "ignored_features": ["reference_transcript"],
            "unsupported_features": ["happy", "sad", "angry", "effective style selector", "speed != 1.0"],
        }
    raise ApiError("unknown_tts_provider", f"Unknown TTS provider '{provider}'", 422, {"provider": provider})
