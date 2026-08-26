import pytest
from pydantic import ValidationError

from app.api.schemas import SpeechRequest
import app.api.routes_audio as routes_audio


def test_speech_rejects_whitespace_only_input() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"input": "   \n\t"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en-US", "en"), ("vi-VN", "vi"), ("VI", "vi"), (" zh-Hant ", "zh")],
)
def test_canonical_language(value: str, expected: str) -> None:
    assert hasattr(routes_audio, "_canonical_language")
    assert routes_audio._canonical_language(value) == expected
