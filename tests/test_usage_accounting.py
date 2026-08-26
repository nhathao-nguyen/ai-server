from app.usage.models import UsageEventInput
from app.usage.service import UsageService


def test_credits_for_combines_token_and_character_usage() -> None:
    service = UsageService(
        None,
        llm_credits_per_1k_tokens=1,
        tts_credits_per_1k_chars=1,
    )
    event = UsageEventInput(
        api_key_id="key",
        endpoint="/test",
        model="model",
        provider="provider",
        input_tokens=1000,
        characters=1000,
    )
    assert service.credits_for(event) == 2
