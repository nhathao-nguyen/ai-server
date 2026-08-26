from app.api.schemas import SpeechRequest


def test_speech_text_value_is_trimmed() -> None:
    payload = SpeechRequest.model_validate({"input": "  hello  "})
    assert payload.text_value == "hello"


def test_tts_headers_are_configured_for_browser_clients() -> None:
    from app.main import create_app
    from fastapi.middleware.cors import CORSMiddleware

    app = create_app()
    middleware = next(item for item in app.user_middleware if item.cls is CORSMiddleware)
    assert "X-TTS-Provider" in middleware.kwargs["expose_headers"]
