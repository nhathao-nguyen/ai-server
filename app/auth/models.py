from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ApiKeyOptions:
    label: str = ""
    owner_note: str = ""
    enabled: bool = True
    expires_at: datetime | None = None
    rate_limit_per_minute: int = 60
    daily_quota_credits: int = 1000
    initial_credits: int = 1000


@dataclass(frozen=True)
class ApiKeyPublic:
    id: str
    key_prefix: str
    label: str
    owner_note: str
    scopes: frozenset[str]
    enabled: bool
    expires_at: datetime | None
    rate_limit_per_minute: int
    daily_quota_credits: int
    credits: int
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool = False


@dataclass(frozen=True)
class ApiKeyPrincipal:
    id: str
    scopes: frozenset[str]
    rate_limit_per_minute: int
    daily_quota_credits: int
    credits: int

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
