import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from app.api.dependencies import AppServices, get_services, require_scope
from app.api.schemas import PreviewRequest, SpeechRequest
from app.auth.models import ApiKeyPrincipal
from app.core.errors import ApiError
from app.engines.base import TtsRequest
from app.engines.tts_options import validate_tts_request
from app.scheduler.jobs import JobSpec
from app.usage.models import UsageEventInput


router = APIRouter(prefix="/v1/audio", tags=["audio"])


def _canonical_language(value: str) -> str:
    normalized = value.strip().lower().split("-", 1)[0]
    if not normalized:
        raise ApiError("invalid_language", "language is required", 422)
    return normalized


def _tts_estimate(text: str) -> int:
    return max(1, (len(text) + 999) // 1000)


async def _generate(
    *,
    text: str,
    language: str,
    model: str | None,
    voice: str | None,
    reference_audio_path: str | None,
    reference_transcript: str | None,
    speed: float,
    options: dict,
    endpoint: str,
    principal: ApiKeyPrincipal,
    services: AppServices,
):
    language = _canonical_language(language)
    route = services.engine_router.resolve_tts(language, model)
    provider = services.vieneu if route.provider == "vieneu" else services.chatterbox
    request = TtsRequest(
        text=text,
        language=language,
        voice=voice,
        reference_audio_path=reference_audio_path,
        reference_transcript=reference_transcript,
        options=options,
        speed=speed,
    )
    normalized_options = validate_tts_request(route.provider, request)
    request = replace(request, options=normalized_options)
    request_id = uuid.uuid4().hex
    reservation = services.usage.reserve(
        principal.id,
        _tts_estimate(text),
        request_id=request_id,
    )
    spec = JobSpec(
        "tts",
        route.provider,
        route.logical_model,
        principal.id,
        route.requires_gpu,
        {"request_id": request_id},
    )
    try:
        result = await services.scheduler.submit(spec, lambda _spec: provider.generate(request))
        event = services.usage.settle(
            reservation,
            UsageEventInput(
                api_key_id=principal.id,
                endpoint=endpoint,
                model=route.logical_model,
                provider=route.provider,
                characters=len(text),
                audio_duration_ms=result.audio_duration_ms,
                processing_ms=result.processing_ms,
                voice=voice,
            ),
        )
        if services.events is not None:
            services.events.publish(
                "usage.settled",
                component="usage",
                provider=route.provider,
                message="TTS usage settled",
                metadata={"endpoint": endpoint, "model": route.logical_model},
            )
    except asyncio.CancelledError as exc:
        services.usage.refund(
            reservation,
            UsageEventInput(
                api_key_id=principal.id,
                endpoint=endpoint,
                model=route.logical_model,
                provider=route.provider,
                status="failed",
                error_code=type(exc).__name__,
                error_message="Request cancelled",
            ),
        )
        raise
    except Exception as exc:
        services.usage.refund(
            reservation,
            UsageEventInput(
                api_key_id=principal.id,
                endpoint=endpoint,
                model=route.logical_model,
                provider=route.provider,
                status="failed",
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(getattr(exc, "message", exc))[:300],
            ),
        )
        raise
    return result, event


def _parse_options_json(raw: str | None) -> dict:
    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid_options_json", "options_json must contain a valid JSON object", 422) from exc
    if not isinstance(value, dict):
        raise ApiError("invalid_options_json", "options_json must contain a JSON object", 422)
    return value


async def _copy_upload_with_cap(upload: UploadFile, destination: Path, max_bytes: int) -> None:
    declared_size = getattr(upload, "size", None)
    if declared_size is not None and declared_size > max_bytes:
        raise ApiError("upload_too_large", "Reference audio exceeds the configured byte limit", 413)
    total = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ApiError("upload_too_large", "Reference audio exceeds the configured byte limit", 413)
            output.write(chunk)


def _audio_response(result, event) -> Response:
    metadata = getattr(result, "metadata", None) or {}
    effective_options = metadata.get("effective_options", {})
    voice = metadata.get("voice") or event.voice or ""
    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Model": event.model,
            "X-TTS-Provider": event.provider,
            "X-TTS-Characters": str(event.characters),
            "X-TTS-Duration-Ms": str(event.audio_duration_ms),
            "X-TTS-Generation-Ms": str(event.processing_ms),
            "X-TTS-Credits": str(event.credits_charged),
            "X-TTS-Voice": quote(str(voice), safe=""),
            "X-TTS-Effective-Options": json.dumps(effective_options, ensure_ascii=True, separators=(",", ":")),
            "X-TTS-Speed": str(metadata.get("speed", 1.0)),
        },
    )


@router.post("/speech")
async def speech(
    payload: SpeechRequest,
    principal: ApiKeyPrincipal = Depends(require_scope("tts.generate")),
    services: AppServices = Depends(get_services),
):
    result, event = await _generate(
        text=payload.text_value,
        language=payload.language,
        model=payload.model,
        voice=payload.voice,
        reference_audio_path=None,
        reference_transcript=None,
        speed=payload.speed,
        options=payload.options,
        endpoint="/v1/audio/speech",
        principal=principal,
        services=services,
    )
    return _audio_response(result, event)


@router.post("/preview")
async def preview(
    payload: PreviewRequest,
    principal: ApiKeyPrincipal = Depends(require_scope("tts.generate")),
    services: AppServices = Depends(get_services),
):
    result, event = await _generate(
        text=payload.text,
        language=payload.language,
        model=payload.model,
        voice=payload.voice,
        reference_audio_path=None,
        reference_transcript=None,
        speed=payload.speed,
        options=payload.options,
        endpoint="/v1/audio/preview",
        principal=principal,
        services=services,
    )
    return _audio_response(result, event)


@router.post("/voice-clone")
async def voice_clone(
    text: str = Form(..., min_length=1, max_length=20_000),
    language: str = Form("en", min_length=2, max_length=32),
    model: str | None = Form(None),
    voice: str | None = Form(None),
    reference_transcript: str | None = Form(None),
    speed: float = Form(1.0, gt=0.1, le=4.0),
    options_json: str | None = Form(None),
    reference_audio: UploadFile = File(...),
    principal: ApiKeyPrincipal = Depends(require_scope("tts.clone")),
    services: AppServices = Depends(get_services),
):
    options = _parse_options_json(options_json)
    suffix = Path(reference_audio.filename or "reference.wav").suffix.lower()
    if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
        suffix = ".wav"
    services.paths.temp.mkdir(parents=True, exist_ok=True)
    temporary_path = services.paths.temp / f"reference-{uuid.uuid4().hex}{suffix}"
    try:
        await _copy_upload_with_cap(reference_audio, temporary_path, services.settings.max_upload_bytes)
        if services.audio_probe is not None:
            duration = await asyncio.to_thread(services.audio_probe, temporary_path)
            if duration > services.settings.max_reference_audio_seconds:
                raise ApiError(
                    "reference_audio_too_long",
                    "Reference audio exceeds the configured duration limit",
                    422,
                )
        result, event = await _generate(
            text=text,
            language=language,
            model=model,
            voice=voice,
            reference_audio_path=str(temporary_path),
            reference_transcript=reference_transcript,
            speed=speed,
            options=options,
            endpoint="/v1/audio/voice-clone",
            principal=principal,
            services=services,
        )
        return _audio_response(result, event)
    finally:
        temporary_path.unlink(missing_ok=True)
