import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.errors import ApiError
from app.engines.base import LlmChunk, LlmRequest, LlmResult


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 900.0,
        keep_alive: str | int = "3600s",
        unload_timeout: float = 10.0,
        unload_poll_interval: float = 0.1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.unload_timeout = unload_timeout
        self.unload_poll_interval = unload_poll_interval
        self._runtime_state = "available"
        self._unload_confirmed = False

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    async def activate(self) -> None:
        # Ollama owns model loading. This hook deliberately does not issue an
        # inference request; the first chat/stream request is the lazy wake.
        if self._runtime_state == "sleeping":
            self._runtime_state = "available"

    async def chat(self, request: LlmRequest) -> LlmResult:
        payload = self._payload(request, stream=False)
        self._runtime_state = "warming"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
        except Exception as exc:
            self._runtime_state = "unavailable"
            raise self._request_error(exc) from exc
        data = response.json()
        self._runtime_state = "sleeping" if self._keep_alive_value() == 0 else "loaded"
        message = data.get("message") or {}
        return LlmResult(
            text=str(message.get("content") or data.get("response") or ""),
            model=str(data.get("model") or self.model),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            processing_ms=self._nanoseconds_to_ms(data.get("total_duration")),
            gpu_time_ms=self._nanoseconds_to_ms(data.get("eval_duration")),
            raw=data,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
        except Exception as exc:
            raise self._request_error(exc) from exc
        data = response.json()
        return list(data.get("models") or [])

    async def health(self) -> dict[str, Any]:
        models = await self.list_models()
        return {"available": True, "model_count": len(models)}

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmChunk]:
        payload = self._payload(request, stream=True)
        self._runtime_state = "warming"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        data = _decode_json_line(line)
                        message = data.get("message") or {}
                        if data.get("done"):
                            self._runtime_state = "sleeping" if self._keep_alive_value() == 0 else "loaded"
                        yield LlmChunk(
                            text=str(message.get("content") or data.get("response") or ""),
                            done=bool(data.get("done")),
                            input_tokens=int(data.get("prompt_eval_count") or 0),
                            output_tokens=int(data.get("eval_count") or 0),
                            processing_ms=self._nanoseconds_to_ms(data.get("total_duration")),
                        )
        except ApiError:
            self._runtime_state = "unavailable"
            raise
        except Exception as exc:
            self._runtime_state = "unavailable"
            raise self._request_error(exc) from exc

    async def unload(self) -> None:
        """Ask Ollama to evict the model and confirm `/api/ps` is empty."""

        payload = {"model": self.model, "prompt": "", "stream": False, "keep_alive": 0}
        self._unload_confirmed = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
        except Exception as exc:
            self._runtime_state = "unavailable"
            raise self._request_error(exc) from exc
        await self.confirm_unloaded()
        self._runtime_state = "sleeping"

    async def confirm_unloaded(self) -> None:
        if self._unload_confirmed:
            return
        started = time.monotonic()
        while True:
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout, 10.0)) as client:
                    response = await client.get(f"{self.base_url}/api/ps")
                    response.raise_for_status()
                models = response.json().get("models") or []
                if not any(self._model_name(item) == self.model for item in models):
                    self._unload_confirmed = True
                    return
            except Exception as exc:
                self._runtime_state = "unavailable"
                raise self._request_error(exc) from exc
            if time.monotonic() - started >= self.unload_timeout:
                raise ApiError("ollama_unload_timeout", "Ollama did not release the model", 504, {"model": self.model})
            await asyncio.sleep(self.unload_poll_interval)

    def _payload(self, request: LlmRequest, *, stream: bool) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": stream,
            "keep_alive": self._keep_alive_value(),
            # Qwen3.5 exposes reasoning separately in Ollama. The gateway
            # presents normal OpenAI-compatible assistant content.
            "think": False,
        }
        if options:
            payload["options"] = options
        return payload

    def _keep_alive_value(self) -> str | int:
        if isinstance(self.keep_alive, str) and self.keep_alive.strip().lstrip("-").isdigit():
            return int(self.keep_alive)
        return self.keep_alive

    @staticmethod
    def _model_name(item: Any) -> str | None:
        if isinstance(item, dict):
            return item.get("name") or item.get("model")
        return getattr(item, "name", None)

    @staticmethod
    def _nanoseconds_to_ms(value: Any) -> int:
        try:
            return round(int(value or 0) / 1_000_000)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _request_error(exc: Exception) -> ApiError:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            code = "ollama_model_missing" if status == 404 else "ollama_error"
            return ApiError(code, "Ollama request failed", 503, {"status_code": status})
        return ApiError("ollama_unavailable", "Ollama is unavailable", 503, {"error": type(exc).__name__})


def _decode_json_line(line: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ApiError("ollama_invalid_stream", "Ollama returned invalid stream data", 502) from exc
    if not isinstance(value, dict):
        raise ApiError("ollama_invalid_stream", "Ollama returned invalid stream data", 502)
    return value
