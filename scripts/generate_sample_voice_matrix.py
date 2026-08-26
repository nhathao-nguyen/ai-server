"""Generate a real server/client voice matrix for the project sample text.

The script deliberately discovers languages and voices from ``/v1/models``;
there is no hard-coded model catalog.  It writes one translation JSON file and
one WAV per selected model/language/voice binding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from scripts.test_server_e2e import SAMPLE_TEXT, _language


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "value"


def _matrix_language(value: str) -> str:
    """Use one canonical output language for ISO-639 aliases."""

    language = _language(value)
    return "vi" if language == "vie" else language


def _call(client: httpx.Client, method: str, path: str, token: str | None = None, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, path, headers=headers, **kwargs)


def discover_voice_matrix(models: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Expand available model capabilities into language/voice bindings."""

    bindings: list[dict[str, str]] = []
    for model in models:
        if not model.get("available"):
            continue
        capabilities = model.get("capabilities") or {}
        languages: list[str] = []
        for raw_language in capabilities.get("supported_languages", []):
            language = _matrix_language(str(raw_language))
            if language and language not in languages:
                languages.append(language)
        preset_voices = [str(item) for item in capabilities.get("preset_voice_names", []) if str(item)]
        if preset_voices:
            # Preset catalogs are language-independent for this provider; keep
            # every ID, but do not emit duplicate canonical language rows.
            for language in languages:
                for voice in preset_voices:
                    bindings.append(
                        {
                            "model": str(model["id"]),
                            "provider": str(model.get("provider") or ""),
                            "language": language,
                            "voice": voice,
                            "voice_catalog": "preset",
                        }
                    )
        else:
            default_voice = str(capabilities.get("default_voice") or "default")
            for language in languages:
                bindings.append(
                    {
                        "model": str(model["id"]),
                        "provider": str(model.get("provider") or ""),
                        "language": language,
                        "voice": default_voice,
                        "voice_catalog": "default",
                    }
                )
    return bindings


def _json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        value = {"raw": response.text[:500]}
    return value if isinstance(value, dict) else {"value": value}


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    admin_key = args.admin_key or os.environ.get("TTS_ADMIN_KEY")
    if not admin_key:
        raise SystemExit("Provide --admin-key or TTS_ADMIN_KEY")
    output_dir = (args.output_dir or Path(tempfile.mkdtemp(prefix="tts-sample-voice-matrix-")).resolve())
    translations_dir = output_dir / "translations"
    audio_dir = output_dir / "audio"
    translations_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "test_type": "real_http_server_and_third_party_voice_matrix",
        "phases": ["server", "client_third_party"],
        "base_url": args.base_url.rstrip("/"),
        "sample_text": args.text.strip() if args.text is not None else SAMPLE_TEXT,
        "server": {},
        "client_third_party": {"translations": {}, "voices": []},
        "artifacts": [],
        "failures": [],
    }
    if not report["sample_text"]:
        raise SystemExit("--text must not be empty")

    timeout = httpx.Timeout(args.timeout)
    admin_headers = {"Authorization": f"Bearer {admin_key}"}
    product_key = None
    product_id = None
    try:
        with httpx.Client(base_url=report["base_url"], timeout=timeout) as client:
            live = _call(client, "GET", "/health/live")
            report["server"]["health_live"] = {"status": live.status_code, "ok": live.status_code == 200}
            models_response = _call(client, "GET", "/v1/models", admin_key)
            report["server"]["model_catalog"] = {"status": models_response.status_code, "ok": models_response.status_code == 200}
            if models_response.status_code != 200:
                report["failures"].append("server_model_catalog")
                return 2, report
            models_payload = _json_response(models_response)
            models = models_payload.get("models", [])
            bindings = discover_voice_matrix(models)
            report["server"]["available_models"] = [
                {"id": item.get("id"), "provider": item.get("provider"), "capabilities": item.get("capabilities", {})}
                for item in models
                if item.get("available")
            ]
            report["client_third_party"]["voice_catalog"] = bindings
            create = _call(
                client,
                "POST",
                "/v1/admin/api-keys",
                admin_key,
                json={
                    "scopes": ["llm.translate", "tts.generate", "usage.read"],
                    "label": "sample-voice-matrix",
                    "owner_note": "temporary real voice matrix test",
                },
            )
            if create.status_code != 200:
                report["failures"].append("create_product_key")
                return 2, report
            created = _json_response(create)
            product_key = str(created.get("key") or "")
            product_id = str(created.get("id") or "")

            language_text: dict[str, str] = {}
            languages = []
            for binding in bindings:
                if binding["language"] not in languages:
                    languages.append(binding["language"])
            for language in languages:
                translation = _call(
                    client,
                    "POST",
                    "/v1/translations",
                    product_key,
                    json={
                        "text": report["sample_text"],
                        "source_language": "vi",
                        "target_language": language,
                        "style": "neutral",
                    },
                )
                payload = _json_response(translation)
                translated = str(payload.get("translation") or "")
                if translation.status_code == 200 and translated:
                    language_text[language] = translated
                else:
                    report["failures"].append(f"translation:{language}")
                translation_file = translations_dir / f"{_safe_name(language)}.json"
                translation_file.write_text(
                    json.dumps(
                        {
                            "phase": "client_third_party",
                            "language": language,
                            "status": translation.status_code,
                            "translated_text": translated,
                            "response": payload,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                report["client_third_party"]["translations"][language] = {
                    "status": translation.status_code,
                    "translated_text": translated,
                    "path": str(translation_file.resolve()),
                }

            for binding in bindings:
                language = binding["language"]
                translated = language_text.get(language)
                item = {**binding, "translation": translated, "status": "pending"}
                if not translated:
                    item["status"] = "skipped_translation_failure"
                    report["client_third_party"]["voices"].append(item)
                    continue
                speech = _call(
                    client,
                    "POST",
                    "/v1/audio/speech",
                    product_key,
                    json={
                        "model": binding["model"],
                        "input": translated,
                        "language": language,
                        "voice": binding["voice"],
                        "response_format": "wav",
                        "speed": 1.0,
                    },
                )
                provider_dir = audio_dir / _safe_name(binding["provider"])
                provider_dir.mkdir(parents=True, exist_ok=True)
                artifact = provider_dir / f"{_safe_name(language)}__{_safe_name(binding['voice'])}.wav"
                if speech.status_code == 200 and speech.content[:4] == b"RIFF":
                    artifact.write_bytes(speech.content)
                    item.update(
                        {
                            "status": "pass",
                            "http_status": speech.status_code,
                            "artifact": str(artifact.resolve()),
                            "bytes": len(speech.content),
                            "provider_header": speech.headers.get("X-TTS-Provider"),
                            "voice_header": speech.headers.get("X-TTS-Voice"),
                        }
                    )
                    report["artifacts"].append(item)
                else:
                    item.update({"status": "failed", "http_status": speech.status_code, "response": _json_response(speech)})
                    report["failures"].append(f"speech:{binding['provider']}:{language}:{binding['voice']}")
                report["client_third_party"]["voices"].append(item)
    finally:
        if product_key and product_id:
            try:
                with httpx.Client(base_url=report["base_url"], timeout=timeout) as cleanup:
                    _call(cleanup, "DELETE", f"/v1/admin/api-keys/{product_id}", admin_key)
            except Exception:
                report["failures"].append("revoke_product_key")

    report["summary"] = {
        "translation_count": len(report["client_third_party"]["translations"]),
        "voice_binding_count": len(bindings),
        "audio_pass_count": sum(item.get("status") == "pass" for item in report["client_third_party"]["voices"]),
        "failure_count": len(report["failures"]),
        "chatterbox_default_voice_reused_across_languages": len(
            {
                (item["language"], item["voice"])
                for item in bindings
                if item["provider"] == "chatterbox"
            }
        )
        > 1,
    }
    report_path = output_dir / "matrix-report.json"
    report["report_path"] = str(report_path.resolve())
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return (0 if not report["failures"] else 2), report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate every available model voice/language artifact for the sample")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-key", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--text", default=None)
    args = parser.parse_args()
    status, report = run(args)
    print(
        json.dumps(
            {
                "status": "pass" if status == 0 else "partial_or_failed",
                "test_type": report["test_type"],
                "phases": report["phases"],
                "report_path": report.get("report_path"),
                "summary": report.get("summary"),
                "failures": report.get("failures", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
