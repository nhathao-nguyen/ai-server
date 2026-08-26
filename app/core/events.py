from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Mapping

from app.workers.protocol import redact_worker_text


_PROMPT_VALUE_RE = re.compile(r"(\b(?:prompt|request_body)\s*[:=]\s*)([^\r\n]*)", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "auth_header",
    "control_token",
    "full_key",
    "key",
    "key_hash",
    "password",
    "prompt",
    "secret",
    "session_token",
    "token",
    "request_body",
    "reference_audio_path",
}


def redact_text(value: str) -> str:
    value = redact_worker_text(value)
    return _PROMPT_VALUE_RE.sub(r"\1[REDACTED]", value)


def _safe_value(value: Any, *, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(name): _safe_value(item, key=str(name)) for name, item in value.items() if str(name).lower() not in _SENSITIVE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


class EventBuffer:
    """Thread-safe bounded lifecycle/event buffer for the local console."""

    def __init__(self, maxlen: int = 500) -> None:
        self.maxlen = max(1, maxlen)
        self._events: deque[dict[str, Any]] = deque(maxlen=self.maxlen)
        self._subscribers: set[EventSubscription] = set()
        self._lock = threading.RLock()
        self._sequence = 0
        self._changed = time.monotonic()

    def publish(
        self,
        event: str,
        *,
        level: str = "INFO",
        component: str = "server",
        provider: str | None = None,
        job_id: str | None = None,
        message: str = "",
        duration_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            record = {
                "id": uuid.uuid4().hex,
                "sequence": self._sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": str(level).upper(),
                "component": component,
                "provider": provider,
                "job_id": job_id,
                "event": event,
                "message": redact_text(message)[:1000],
                "duration_ms": duration_ms,
                "metadata": _safe_value(dict(metadata or {})),
            }
            self._events.append(record)
            for subscriber in tuple(self._subscribers):
                subscriber.push(record)
            self._changed = time.monotonic()
            return dict(record)

    def subscribe(self, maxsize: int = 128) -> "EventSubscription":
        subscriber = EventSubscription(self, maxsize=maxsize)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def _unsubscribe(self, subscriber: "EventSubscription") -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def recent(self, limit: int = 200, *, after: int | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, self.maxlen))
        with self._lock:
            records = list(self._events)
        if after is not None:
            records = [item for item in records if int(item["sequence"]) > after]
        return records[-bounded_limit:]

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._changed = time.monotonic()


class EventSubscription:
    def __init__(self, buffer: EventBuffer, *, maxsize: int) -> None:
        self._buffer = buffer
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, maxsize))
        self._closed = False

    def push(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(dict(event))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(dict(event))

    def get(self, timeout: float | None = None) -> dict[str, Any]:
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buffer._unsubscribe(self)
