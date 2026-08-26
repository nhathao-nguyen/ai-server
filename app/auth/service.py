import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Iterable

from app.auth.models import ApiKeyOptions, ApiKeyPrincipal, ApiKeyPublic
from app.core.database import Database
from app.core.errors import ApiError


AVAILABLE_SCOPES = frozenset(
    {
        "admin.full",
        "llm.generate",
        "llm.translate",
        "tts.generate",
        "tts.clone",
        "usage.read",
    }
)


def _valid_scopes_from_json(value: str) -> frozenset[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(decoded, list):
        return frozenset()
    return frozenset(
        scope for scope in decoded if isinstance(scope, str) and scope in AVAILABLE_SCOPES
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ApiKeyService:
    def __init__(
        self,
        db: Database,
        default_rate_limit_per_minute: int = 60,
        default_daily_quota_credits: int = 1000,
        default_initial_credits: int = 1000,
    ) -> None:
        self.db = db
        self.default_options = ApiKeyOptions(
            rate_limit_per_minute=default_rate_limit_per_minute,
            daily_quota_credits=default_daily_quota_credits,
            initial_credits=default_initial_credits,
        )

    def sanitize_legacy_keys(self) -> dict[str, int]:
        """Remove unsupported scopes while preserving valid admin keys."""

        result = {"updated": 0, "removed": 0, "tombstoned": 0}
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT id, scopes_json FROM api_keys").fetchall()
            for row in rows:
                valid_scopes = sorted(_valid_scopes_from_json(row["scopes_json"]))
                try:
                    decoded = json.loads(row["scopes_json"])
                except (TypeError, json.JSONDecodeError):
                    decoded = None
                stored_scopes = (
                    sorted({scope for scope in decoded if isinstance(scope, str)})
                    if isinstance(decoded, list)
                    else []
                )
                if stored_scopes == valid_scopes:
                    continue

                if valid_scopes:
                    connection.execute(
                        "UPDATE api_keys SET scopes_json = ? WHERE id = ?",
                        (json.dumps(valid_scopes), row["id"]),
                    )
                    result["updated"] += 1
                    continue

                referenced = connection.execute(
                    """
                    SELECT
                        EXISTS(SELECT 1 FROM usage_events WHERE api_key_id = ?) OR
                        EXISTS(SELECT 1 FROM jobs WHERE api_key_id = ?) OR
                        EXISTS(SELECT 1 FROM credit_ledger WHERE api_key_id = ?) OR
                        EXISTS(SELECT 1 FROM credit_reservations WHERE api_key_id = ?)
                        AS present
                    """,
                    (row["id"], row["id"], row["id"], row["id"]),
                ).fetchone()["present"]
                if referenced:
                    connection.execute(
                        """
                        UPDATE api_keys
                        SET scopes_json = '[]', enabled = 0, revoked_at = COALESCE(revoked_at, ?)
                        WHERE id = ?
                        """,
                        (to_iso(utc_now()), row["id"]),
                    )
                    result["tombstoned"] += 1
                else:
                    connection.execute("DELETE FROM api_keys WHERE id = ?", (row["id"],))
                    result["removed"] += 1
        return result

    def create(
        self,
        scopes: Iterable[str],
        options: ApiKeyOptions | None = None,
        *,
        enabled: bool | None = None,
        label: str | None = None,
        owner_note: str | None = None,
        expires_at: datetime | None = None,
        rate_limit_per_minute: int | None = None,
        daily_quota_credits: int | None = None,
        initial_credits: int | None = None,
    ) -> tuple[ApiKeyPublic, str]:
        base = options or self.default_options
        selected = ApiKeyOptions(
            label=base.label if label is None else label.strip(),
            owner_note=base.owner_note if owner_note is None else owner_note.strip(),
            enabled=base.enabled if enabled is None else enabled,
            expires_at=base.expires_at if expires_at is None else expires_at,
            rate_limit_per_minute=(
                base.rate_limit_per_minute
                if rate_limit_per_minute is None
                else rate_limit_per_minute
            ),
            daily_quota_credits=(
                base.daily_quota_credits
                if daily_quota_credits is None
                else daily_quota_credits
            ),
            initial_credits=base.initial_credits if initial_credits is None else initial_credits,
        )
        if selected.rate_limit_per_minute < 1 or selected.daily_quota_credits < 0 or selected.initial_credits < 0:
            raise ApiError("invalid_api_key_options", "API key limits must be valid", 422)
        if len(selected.label) > 128 or len(selected.owner_note) > 500:
            raise ApiError("invalid_api_key_options", "API key label or owner note is too long", 422)

        full_key = "ai_sk_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(full_key.encode("utf-8")).hexdigest()
        key_id = uuid.uuid4().hex
        created_at = utc_now()
        normalized_scopes = sorted({scope.strip() for scope in scopes if scope.strip()})
        unknown_scopes = sorted(set(normalized_scopes) - AVAILABLE_SCOPES)
        if not normalized_scopes or unknown_scopes:
            raise ApiError(
                "invalid_scopes",
                "API key contains unsupported scope(s)",
                422,
                {"scopes": unknown_scopes},
            )
        self.db.execute(
            """
            INSERT INTO api_keys (
                id, key_hash, key_prefix, scopes_json, label, owner_note, enabled, expires_at,
                rate_limit_per_minute, daily_quota_credits, credits, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                key_hash,
                full_key[:14],
                json.dumps(normalized_scopes),
                selected.label,
                selected.owner_note,
                int(selected.enabled),
                to_iso(selected.expires_at) if selected.expires_at else None,
                selected.rate_limit_per_minute,
                selected.daily_quota_credits,
                selected.initial_credits,
                to_iso(created_at),
            ),
        )
        return self._public_from_row(self.db.fetch_one("SELECT * FROM api_keys WHERE id = ?", (key_id,))), full_key

    def authenticate(self, full_key: str) -> ApiKeyPrincipal:
        if not full_key or not full_key.startswith("ai_sk_"):
            raise ApiError("authentication_required", "A valid bearer API key is required", 401)
        key_hash = hashlib.sha256(full_key.encode("utf-8")).hexdigest()
        row = self.db.fetch_one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        if row is None:
            raise ApiError("invalid_api_key", "The API key is invalid", 401)
        if not row["enabled"]:
            raise ApiError("api_key_disabled", "The API key is disabled", 403)
        if row["revoked_at"] is not None:
            raise ApiError("api_key_revoked", "The API key is revoked", 403)
        expires_at = from_iso(row["expires_at"])
        if expires_at is not None and expires_at <= utc_now():
            raise ApiError("api_key_expired", "The API key has expired", 403)
        self.db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (to_iso(utc_now()), row["id"]))
        return ApiKeyPrincipal(
            id=row["id"],
        scopes=_valid_scopes_from_json(row["scopes_json"]),
            rate_limit_per_minute=row["rate_limit_per_minute"],
            daily_quota_credits=row["daily_quota_credits"],
            credits=row["credits"],
        )

    def get_public(self, key_id: str) -> ApiKeyPublic | None:
        row = self.db.fetch_one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        return self._public_from_row(row) if row else None

    def list_public(self, *, include_inactive: bool = False) -> list[ApiKeyPublic]:
        rows = self.db.fetch_all("SELECT * FROM api_keys ORDER BY created_at ASC")
        public = [self._public_from_row(row) for row in rows]
        if include_inactive:
            return [item for item in public if item.scopes]
        now = utc_now()
        return [
            item
            for item in public
            if item.scopes
            and item.enabled
            and not item.revoked
            and (item.expires_at is None or item.expires_at > now)
        ]

    def set_enabled(self, key_id: str, enabled: bool) -> ApiKeyPublic:
        row = self.db.fetch_one("SELECT revoked_at FROM api_keys WHERE id = ?", (key_id,))
        if row is None:
            raise ApiError("api_key_not_found", "API key was not found", 404)
        if enabled and row["revoked_at"] is not None:
            raise ApiError("api_key_revoked", "A revoked API key cannot be enabled", 409)
        cursor = self.db.execute("UPDATE api_keys SET enabled = ? WHERE id = ?", (int(enabled), key_id))
        if cursor.rowcount != 1:
            raise ApiError("api_key_not_found", "API key was not found", 404)
        return self.get_public(key_id)  # type: ignore[return-value]

    def revoke(self, key_id: str) -> ApiKeyPublic:
        cursor = self.db.execute(
            "UPDATE api_keys SET enabled = 0, revoked_at = ? WHERE id = ?",
            (to_iso(utc_now()), key_id),
        )
        if cursor.rowcount != 1:
            raise ApiError("api_key_not_found", "API key was not found", 404)
        return self.get_public(key_id)  # type: ignore[return-value]

    def delete_permanently(self, key_id: str) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT enabled, expires_at, revoked_at FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if row is None:
                raise ApiError("api_key_not_found", "API key was not found", 404)

            expires_at = from_iso(row["expires_at"])
            active = bool(row["enabled"]) and row["revoked_at"] is None and (
                expires_at is None or expires_at > utc_now()
            )
            if active:
                raise ApiError(
                    "api_key_active",
                    "An active API key must be disabled, expired, or revoked before permanent deletion",
                    409,
                )

            referenced = connection.execute(
                """
                SELECT
                    EXISTS(SELECT 1 FROM usage_events WHERE api_key_id = ?) OR
                    EXISTS(SELECT 1 FROM jobs WHERE api_key_id = ?) OR
                    EXISTS(SELECT 1 FROM credit_ledger WHERE api_key_id = ?) OR
                    EXISTS(SELECT 1 FROM credit_reservations WHERE api_key_id = ?)
                    AS present
                """,
                (key_id, key_id, key_id, key_id),
            ).fetchone()["present"]
            if referenced:
                raise ApiError(
                    "api_key_has_history",
                    "This API key has historical records and cannot be permanently deleted",
                    409,
                )

            connection.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))

    @staticmethod
    def _public_from_row(row) -> ApiKeyPublic:
        return ApiKeyPublic(
            id=row["id"],
            key_prefix=row["key_prefix"],
            label=row["label"],
            owner_note=row["owner_note"],
            scopes=_valid_scopes_from_json(row["scopes_json"]),
            enabled=bool(row["enabled"]),
            expires_at=from_iso(row["expires_at"]),
            rate_limit_per_minute=row["rate_limit_per_minute"],
            daily_quota_credits=row["daily_quota_credits"],
            credits=row["credits"],
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
            last_used_at=from_iso(row["last_used_at"]),
            revoked=row["revoked_at"] is not None,
        )
