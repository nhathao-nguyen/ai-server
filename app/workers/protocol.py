"""Redacted JSONL error contract shared by model workers."""

from __future__ import annotations

import re


_TOKEN_RE = re.compile(r"(?:Bearer\s+[^\s,;]+|(?:ai_sk_|dctl_|dses_)[A-Za-z0-9_-]+)", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\|/)[^\s\"']+")


def safe_worker_error(error: BaseException) -> str:
    """Return a stable error identifier without forwarding exception text."""

    code = getattr(error, "code", None)
    if isinstance(code, str) and code and all(character.isalnum() or character in "._-" for character in code):
        return code[:120]
    return type(error).__name__[:120]


def redact_worker_text(value: str) -> str:
    """Keep bounded worker diagnostics without forwarding paths or tokens."""

    value = _TOKEN_RE.sub("[REDACTED]", value)
    return _PATH_RE.sub("[PATH]", value)[:500]
