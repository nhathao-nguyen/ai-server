from scripts.generate_sample_voice_matrix import discover_voice_matrix
from scripts.test_server_e2e import collect_supported_languages, select_voice_bindings


def test_collect_supported_languages_uses_available_model_capabilities() -> None:
    models = [
        {
            "id": "disabled",
            "available": False,
            "capabilities": {"supported_languages": ["fr"]},
        },
        {
            "id": "vi",
            "available": True,
            "capabilities": {"supported_languages": ["vi", "vi-VN"]},
        },
        {
            "id": "en",
            "available": True,
            "capabilities": {"supported_languages": ["en"]},
        },
    ]
    assert collect_supported_languages(models) == ["en", "vi"]


def test_voice_bindings_are_unique_and_match_model_language_capability() -> None:
    models = [
        {
            "id": "vi-model",
            "provider": "vieneu",
            "available": True,
            "capabilities": {
                "supported_languages": ["vi"],
                "preset_voice_names": ["Voice A"],
                "default_voice": "Voice A",
            },
        },
        {
            "id": "en-model",
            "provider": "chatterbox",
            "available": True,
            "capabilities": {
                "supported_languages": ["en", "fr"],
                "preset_voice_names": [],
                "default_voice": "default",
            },
        },
    ]
    bindings = select_voice_bindings(models, ("vi-VN", "en", "fr"))
    assert [(item["language"], item["model"], item["voice"]) for item in bindings] == [
        ("vi", "vi-model", "Voice A"),
        ("en", "en-model", "default"),
    ]
    assert len({item["voice"] for item in bindings}) == len(bindings)


def test_voice_matrix_expands_every_preset_and_default_language() -> None:
    models = [
        {
            "id": "vi-model",
            "provider": "vieneu",
            "available": True,
            "capabilities": {
                "supported_languages": ["vi", "vie"],
                "preset_voice_names": ["Voice A", "Voice B"],
            },
        },
        {
            "id": "multi-model",
            "provider": "chatterbox",
            "available": True,
            "capabilities": {
                "supported_languages": ["en", "fr"],
                "default_voice": "default",
            },
        },
    ]
    matrix = discover_voice_matrix(models)
    assert [(row["language"], row["voice"]) for row in matrix] == [
        ("vi", "Voice A"),
        ("vi", "Voice B"),
        ("en", "default"),
        ("fr", "default"),
    ]
