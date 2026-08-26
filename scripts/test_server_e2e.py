"""Run a real server/client end-to-end smoke test without exposing API secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx


SAMPLE_TEXT = (
    'Sáng nay, lúc 7 giờ 45 phút, Minh vui vẻ nói: “Nếu ngày mai trời đẹp, '
    'chúng ta sẽ đi uống cà phê, thử món bánh mới và chụp vài bức ảnh bên bờ sông nhé!”'
)
PRODUCT_SCOPES = {"llm.generate", "llm.translate", "tts.generate", "usage.read"}


def _language(value: str) -> str:
    return value.strip().lower().split("-", 1)[0]


def collect_supported_languages(models: list[dict[str, Any]]) -> list[str]:
    """Return canonical languages advertised by available model capabilities."""

    languages: set[str] = set()
    for model in models:
        if not model.get("available"):
            continue
        capabilities = model.get("capabilities") or {}
        languages.update(
            _language(str(item))
            for item in capabilities.get("supported_languages", [])
            if str(item).strip()
        )
    return sorted(languages)


def select_voice_bindings(models: list[dict[str, Any]], languages: tuple[str, ...]) -> list[dict[str, Any]]:
    """Select available model/voice pairs, never reusing a voice ID."""

    bindings: list[dict[str, Any]] = []
    used_voices: set[str] = set()
    seen_languages: set[str] = set()
    for requested in languages:
        language = _language(requested)
        if not language or language in seen_languages:
            continue
        seen_languages.add(language)
        candidates = []
        for model in models:
            if not model.get("available"):
                continue
            capabilities = model.get("capabilities") or {}
            supported = {_language(str(item)) for item in capabilities.get("supported_languages", [])}
            if language in supported:
                candidates.append((model, capabilities))
        selected: dict[str, Any] | None = None
        for model, capabilities in candidates:
            preset_names = [str(item) for item in capabilities.get("preset_voice_names", [])]
            voice_candidates = preset_names + [str(capabilities.get("default_voice") or "default")]
            for voice in voice_candidates:
                if voice and voice not in used_voices:
                    selected = {
                        "language": language,
                        "model": model["id"],
                        "provider": model.get("provider"),
                        "voice": voice,
                        "voice_catalog": "preset" if voice in preset_names else "default",
                    }
                    break
            if selected:
                break
        if selected:
            used_voices.add(selected["voice"])
            bindings.append(selected)
    return bindings


def validate_voice_bindings(bindings: list[dict[str, Any]]) -> None:
    """Fail closed if the client would reuse a voice ID across languages."""

    voices = [str(item.get("voice") or "") for item in bindings]
    if any(not voice for voice in voices) or len(set(voices)) != len(voices):
        raise ValueError("voice IDs must be non-empty and unique across languages")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "value"


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def _check(response: httpx.Response, expected: int = 200) -> dict[str, Any]:
    payload = _json_or_text(response)
    return {
        "status": "pass" if response.status_code == expected else "fail",
        "http_status": response.status_code,
        "expected_status": expected,
        "response": payload,
    }


def _call(client: httpx.Client, method: str, path: str, token: str | None = None, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, path, headers=headers, **kwargs)


def summarize_failures(server_checks: dict[str, Any], client_checks: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    for section in (server_checks, client_checks):
        failed.extend(
            name
            for name, result in section.items()
            if isinstance(result, dict) and result.get("status") == "fail"
        )
    if not client_checks.get("voice_bindings"):
        failed.append("no_available_voice_binding")
    return failed


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    admin_key = args.admin_key or os.environ.get("TTS_ADMIN_KEY")
    if not admin_key:
        raise SystemExit("Provide --admin-key or TTS_ADMIN_KEY")
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="tts-server-e2e-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_text = args.text.strip() if args.text is not None else SAMPLE_TEXT
    if not sample_text:
        raise SystemExit("--text must not be empty")
    report: dict[str, Any] = {
        "test_type": "real_http_server_and_third_party_client",
        "base_url": args.base_url.rstrip("/"),
        "phases": ["server", "client_third_party"],
        "server_checks": {},
        "client_checks": {},
        "artifacts": [],
        "sample_text": sample_text,
        "output_dir": str(output_dir.resolve()),
    }

    timeout = httpx.Timeout(args.timeout)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=timeout) as client:
        live = _call(client, "GET", "/health/live")
        report["server_checks"]["health_live"] = _check(live)
        if live.status_code != 200:
            return 1, report

        admin_paths = (
            ("overview", "GET", "/v1/admin/overview"),
            ("scopes", "GET", "/v1/admin/scopes"),
            ("build_info", "GET", "/v1/admin/build-info"),
            ("api_keys", "GET", "/v1/admin/api-keys?include_inactive=true"),
            ("gpu", "GET", "/v1/admin/gpu"),
            ("metrics", "GET", "/v1/admin/metrics"),
            ("jobs", "GET", "/v1/admin/jobs"),
            ("models", "GET", "/v1/admin/models"),
            ("events", "GET", "/v1/admin/events"),
        )
        for name, method, path in admin_paths:
            response = _call(client, method, path, admin_key)
            report["server_checks"][name] = _check(response)

        models_response = _call(client, "GET", "/v1/models", admin_key)
        report["server_checks"]["model_catalog"] = _check(models_response)
        if models_response.status_code != 200:
            return 1, report
        model_payload = _json_or_text(models_response)
        models = model_payload.get("models", []) if isinstance(model_payload, dict) else []

        create = _call(
            client,
            "POST",
            "/v1/admin/api-keys",
            admin_key,
            json={
                "scopes": sorted(PRODUCT_SCOPES),
                "label": "e2e-third-party-client",
                "owner_note": "temporary real server/client smoke test",
            },
        )
        report["server_checks"]["create_product_api_key"] = _check(create)
        if create.status_code != 200:
            return 1, report
        created = _json_or_text(create)
        product_key = str(created.get("key") or "")
        product_id = str(created.get("id") or "")
        report["server_checks"]["created_key_metadata"] = {
            "id": product_id,
            "key_prefix": created.get("key_prefix"),
            "scopes": created.get("scopes"),
        }

        disable = _call(client, "POST", f"/v1/admin/api-keys/{product_id}/disable", admin_key)
        enable = _call(client, "POST", f"/v1/admin/api-keys/{product_id}/enable", admin_key)
        report["server_checks"]["disable_product_key"] = _check(disable)
        report["server_checks"]["enable_product_key"] = _check(enable)

        third_party_models = _call(client, "GET", "/v1/models", product_key)
        report["client_checks"]["third_party_model_catalog"] = _check(third_party_models)
        requested_languages = [_language(language) for language in args.languages]
        if args.all_supported:
            discovered = collect_supported_languages(models)
            requested_languages = list(dict.fromkeys(requested_languages + discovered))
        bindings = select_voice_bindings(models, tuple(requested_languages))
        validate_voice_bindings(bindings)
        report["client_checks"]["voice_bindings"] = bindings
        report["client_checks"]["requested_languages"] = requested_languages
        report["client_checks"]["skipped_languages"] = [
            _language(language)
            for language in requested_languages
            if _language(language) not in {item["language"] for item in bindings}
        ]

        translations: dict[str, dict[str, Any]] = {}
        for language in requested_languages:
            translation = _call(
                client,
                "POST",
                "/v1/translations",
                product_key,
                json={
                    "text": sample_text,
                    "source_language": "vi",
                    "target_language": language,
                    "style": "neutral",
                },
            )
            translation_result = _check(translation)
            if translation.status_code == 200:
                translation_payload = _json_or_text(translation)
                translation_result["translated_text"] = (
                    str(translation_payload.get("translation") or sample_text)
                    if isinstance(translation_payload, dict)
                    else sample_text
                )
            translations[language] = translation_result
        report["client_checks"]["translations"] = translations

        for binding in bindings:
            language = binding["language"]
            translation_result = translations.get(language, {})
            translated_text = str(translation_result.get("translated_text") or sample_text)
            if translation_result.get("status") != "pass":
                continue
            speech = _call(
                client,
                "POST",
                "/v1/audio/speech",
                product_key,
                json={
                    "model": binding["model"],
                    "input": translated_text,
                    "language": language,
                    "voice": binding["voice"],
                    "response_format": "wav",
                    "speed": 1.0,
                },
            )
            speech_result = _check(speech)
            speech_result["provider_header"] = speech.headers.get("X-TTS-Provider")
            speech_result["voice_header"] = speech.headers.get("X-TTS-Voice")
            report["client_checks"].setdefault("speech", {})[language] = speech_result
            if speech.status_code == 200 and speech.content[:4] == b"RIFF":
                artifact = output_dir / f"speech-{_safe_name(language)}-{_safe_name(binding['voice'])}.wav"
                artifact.write_bytes(speech.content)
                report["artifacts"].append({"language": language, "voice": binding["voice"], "path": str(artifact.resolve()), "bytes": len(speech.content)})

        llm = _call(
            client,
            "POST",
            "/v1/chat/completions",
            product_key,
            json={
                "model": "llm-default",
                "messages": [{"role": "user", "content": f"Tóm tắt ngắn câu sau bằng tiếng Việt: {sample_text}"}],
                "stream": False,
                "max_tokens": 128,
            },
        )
        report["client_checks"]["llm_chat"] = _check(llm)
        if llm.status_code == 200:
            llm_payload = _json_or_text(llm)
            report["client_checks"]["llm_result"] = {
                "text": llm_payload.get("choices", [{}])[0].get("message", {}).get("content", "")
                if isinstance(llm_payload, dict)
                else "",
                "usage": llm_payload.get("usage") if isinstance(llm_payload, dict) else None,
            }

        report["server_checks"]["usage_after_client_calls"] = _check(_call(client, "GET", "/v1/admin/usage", admin_key))
        report["server_checks"]["metrics_after_client_calls"] = _check(_call(client, "GET", "/v1/admin/metrics", admin_key))
        report["server_checks"]["backup"] = _check(_call(client, "POST", "/v1/admin/backup", admin_key))
        report["server_checks"]["runtime_reset"] = _check(_call(client, "POST", "/v1/admin/runtime/reset", admin_key))
        revoke = _call(client, "DELETE", f"/v1/admin/api-keys/{product_id}", admin_key)
        report["server_checks"]["revoke_product_key"] = _check(revoke)
        revoked_use = _call(client, "GET", "/v1/models", product_key)
        report["client_checks"]["revoked_key_rejected"] = _check(revoked_use, expected=403)

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path.resolve())
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["failed_checks"] = summarize_failures(report["server_checks"], report["client_checks"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return (0 if not report["failed_checks"] else 2), report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run a real HTTP server plus third-party client smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-key", default=None, help="Admin key; prefer TTS_ADMIN_KEY to avoid shell history")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["vi", "en"],
        help="Canonical/language-tag inputs to test (default: vi en)",
    )
    parser.add_argument(
        "--all-supported",
        action="store_true",
        help="Discover all languages from available model capabilities; TTS runs only where a unique voice exists",
    )
    parser.add_argument("--text", default=None, help="Text to translate and synthesize; default is the project sample")
    args = parser.parse_args()
    status, report = run(args)
    print(json.dumps({
        "status": "pass" if status == 0 else "partial_or_failed",
        "test_type": report["test_type"],
        "phases": report.get("phases", []),
        "sample_text": report.get("sample_text"),
        "report_path": report.get("report_path"),
        "artifacts": report.get("artifacts", []),
        "voice_bindings": report.get("client_checks", {}).get("voice_bindings", []),
        "translations": report.get("client_checks", {}).get("translations", {}),
        "failed_checks": report.get("failed_checks", []),
    }, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
