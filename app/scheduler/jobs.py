from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JobSpec:
    kind: str
    provider: str
    model: str
    api_key_id: str | None
    requires_gpu: bool
    metadata: dict[str, Any] = field(default_factory=dict)
