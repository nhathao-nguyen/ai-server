import logging
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.workers.protocol import redact_worker_text


_PROMPT_VALUE_RE = re.compile(r"(\b(?:prompt|request_body)\s*[:=]\s*)([^\r\n]*)", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "auth_header",
    "control_token",
    "full_key",
    "key_hash",
    "password",
    "prompt",
    "reference_audio_path",
    "secret",
    "session_token",
    "token",
}
_SEQUENCE = 0
_SEQUENCE_LOCK = threading.Lock()


def _redact_log_text(value: str) -> str:
    value = redact_worker_text(value)
    return _PROMPT_VALUE_RE.sub(r"\1[REDACTED]", value)


def _safe_metadata(value):
    if isinstance(value, dict):
        return {
            str(key): _safe_metadata(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item) for item in value]
    if isinstance(value, str):
        return _redact_log_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_log_text(str(value))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_log_text(record.getMessage())
        record.args = ()
        if hasattr(record, "metadata"):
            record.metadata = _safe_metadata(record.metadata)
        return True


class JsonEventFormatter(logging.Formatter):
    """Render disk logs using the same bounded event shape as the admin API."""

    def format(self, record: logging.LogRecord) -> str:
        global _SEQUENCE
        with _SEQUENCE_LOCK:
            _SEQUENCE += 1
            sequence = _SEQUENCE
        payload = {
            "id": uuid.uuid4().hex,
            "sequence": sequence,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "component": record.name,
            "provider": getattr(record, "provider", None),
            "job_id": getattr(record, "job_id", None),
            "event": getattr(record, "event", record.getMessage().split(";", 1)[0][:120]),
            "message": _redact_log_text(record.getMessage())[:1000],
            "duration_ms": getattr(record, "duration_ms", None),
            "metadata": _safe_metadata(getattr(record, "metadata", {})),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SizeAndTimeRotatingFileHandler(RotatingFileHandler):
    """Rotate JSONL logs at the size limit or the next local midnight."""

    def __init__(self, filename, *, maxBytes: int, backupCount: int, **kwargs) -> None:
        super().__init__(filename, maxBytes=maxBytes, backupCount=backupCount, **kwargs)
        self._next_midnight = self._calculate_next_midnight()

    @staticmethod
    def _calculate_next_midnight() -> float:
        now = datetime.now().astimezone()
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo).timestamp()

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802 - logging API name
        if time.time() >= self._next_midnight:
            return 1
        if self.maxBytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        encoded_message = (record.getMessage() + self.terminator).encode(self.encoding or "utf-8", errors="replace")
        return int(self.stream.tell() + len(encoded_message) >= self.maxBytes)

    def doRollover(self) -> None:  # noqa: N802 - logging API name
        super().doRollover()
        self._next_midnight = self._calculate_next_midnight()


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("tts_server")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = SizeAndTimeRotatingFileHandler(
                log_dir / "tts-server.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(JsonEventFormatter())
            file_handler.addFilter(RedactingFilter())
            logger.addHandler(file_handler)
    logger.propagate = False
    return logger
