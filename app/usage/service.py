import math
import uuid
from datetime import datetime, timezone

from app.core.database import Database
from app.core.errors import ApiError
from app.usage.models import CreditReservation, UsageEvent, UsageEventInput
from app.usage.repository import UsageRepository, utc_iso


class UsageService:
    def __init__(
        self,
        db: Database | None = None,
        *,
        llm_credits_per_1k_tokens: int = 1,
        tts_credits_per_1k_chars: int = 1,
    ) -> None:
        self.db = db
        self.llm_credits_per_1k_tokens = llm_credits_per_1k_tokens
        self.tts_credits_per_1k_chars = tts_credits_per_1k_chars
        self.repository = UsageRepository(db) if db else None

    def reserve(
        self,
        api_key_id: str,
        estimated_credits: int,
        *,
        request_id: str | None = None,
    ) -> CreditReservation:
        if self.db is None:
            raise ApiError("usage_not_configured", "Usage database is not configured", 500)
        amount = max(0, int(estimated_credits))
        request_id = request_id or uuid.uuid4().hex
        reservation_id = uuid.uuid4().hex
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM credit_reservations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["api_key_id"] != api_key_id:
                    raise ApiError("reservation_conflict", "Request ID is already reserved", 409)
                return self._reservation_from_row(existing)

            now = utc_iso()
            current = datetime.fromisoformat(now)
            start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            key = connection.execute(
                "SELECT credits, daily_quota_credits FROM api_keys WHERE id = ?",
                (api_key_id,),
            ).fetchone()
            if key is None:
                raise ApiError("api_key_not_found", "API key was not found", 404)
            settled = connection.execute(
                """
                SELECT COALESCE(SUM(credits_charged), 0) AS total
                FROM usage_events
                WHERE api_key_id = ? AND status = 'succeeded' AND created_at >= ?
                """,
                (api_key_id, start),
            ).fetchone()["total"]
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM credit_reservations
                WHERE api_key_id = ? AND state = 'reserved' AND created_at >= ?
                """,
                (api_key_id, start),
            ).fetchone()["total"]
            if int(settled) + int(reserved) + amount > int(key["daily_quota_credits"]):
                raise ApiError("daily_quota_exceeded", "API key daily credit quota is exhausted", 429)

            cursor = connection.execute(
                "UPDATE api_keys SET credits = credits - ? WHERE id = ? AND credits >= ?",
                (amount, api_key_id, amount),
            )
            if cursor.rowcount != 1:
                raise ApiError("credits_exhausted", "API key does not have enough credits", 402)
            connection.execute(
                "INSERT INTO credit_ledger (api_key_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
                (api_key_id, -amount, f"reserve:{reservation_id}", now),
            )
            connection.execute(
                """
                INSERT INTO credit_reservations
                    (id, request_id, api_key_id, amount, state, created_at)
                VALUES (?, ?, ?, ?, 'reserved', ?)
                """,
                (reservation_id, request_id, api_key_id, amount, now),
            )
        return CreditReservation(reservation_id, api_key_id, amount, request_id, "reserved")

    def settle(self, reservation: CreditReservation, event_input: UsageEventInput) -> UsageEvent:
        if self.db is None:
            raise ApiError("usage_not_configured", "Usage database is not configured", 500)
        return self._finalize(reservation, event_input, terminal_state="settled")

    def refund(self, reservation: CreditReservation, event_input: UsageEventInput) -> UsageEvent:
        return self._finalize(reservation, event_input, terminal_state="refunded")

    def _finalize(
        self,
        reservation: CreditReservation,
        event_input: UsageEventInput,
        *,
        terminal_state: str,
    ) -> UsageEvent:
        if self.db is None:
            raise ApiError("usage_not_configured", "Usage database is not configured", 500)
        actual = 0 if terminal_state == "refunded" else self.credits_for(event_input)
        adjustment = reservation.amount - actual
        with self.db.transaction() as connection:
            stored = connection.execute(
                "SELECT * FROM credit_reservations WHERE id = ?",
                (reservation.id,),
            ).fetchone()
            if stored is None:
                raise ApiError("reservation_not_found", "Credit reservation was not found", 404)
            if stored["state"] != "reserved":
                event = (
                    connection.execute(
                        "SELECT * FROM usage_events WHERE id = ?",
                        (stored["usage_event_id"],),
                    ).fetchone()
                    if stored["usage_event_id"] is not None
                    else None
                )
                if event is None:
                    raise ApiError("reservation_already_finalized", "Credit reservation is already finalized", 409)
                return self._event_from_row(event)
            if adjustment:
                cursor = connection.execute(
                    "UPDATE api_keys SET credits = credits + ? WHERE id = ? AND credits + ? >= 0",
                    (adjustment, reservation.api_key_id, adjustment),
                )
                if cursor.rowcount != 1:
                    raise ApiError("credits_floor", "Credit settlement would make balance negative", 409)
                connection.execute(
                    "INSERT INTO credit_ledger (api_key_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
                    (reservation.api_key_id, adjustment, f"settle:{reservation.id}", utc_iso()),
                )
            cursor = connection.execute(
                """
                INSERT INTO usage_events (
                    api_key_id, endpoint, model, provider, input_tokens, output_tokens,
                    characters, audio_duration_ms, processing_ms, gpu_time_ms, voice,
                    credits_charged, status, error_code, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation.api_key_id,
                    event_input.endpoint,
                    event_input.model,
                    event_input.provider,
                    event_input.input_tokens,
                    event_input.output_tokens,
                    event_input.characters,
                    event_input.audio_duration_ms,
                    event_input.processing_ms,
                    event_input.gpu_time_ms,
                    event_input.voice,
                    actual,
                    event_input.status,
                    event_input.error_code,
                    event_input.error_message,
                    utc_iso(),
                ),
            )
            event_id = cursor.lastrowid
            connection.execute(
                "UPDATE credit_ledger SET usage_event_id = ? WHERE api_key_id = ? AND reason = ?",
                (event_id, reservation.api_key_id, f"reserve:{reservation.id}"),
            )
            connection.execute(
                "UPDATE credit_reservations SET state = ?, settled_at = ?, usage_event_id = ? WHERE id = ?",
                (terminal_state, utc_iso(), event_id, reservation.id),
            )
            row = connection.execute("SELECT * FROM usage_events WHERE id = ?", (event_id,)).fetchone()
        return self._event_from_row(row)

    def record_event(self, event_input: UsageEventInput) -> UsageEvent:
        reservation = self.reserve(event_input.api_key_id, self.credits_for(event_input))
        return self.settle(reservation, event_input)

    def credits_for(self, event_input: UsageEventInput) -> int:
        token_credits = 0
        token_count = max(0, event_input.input_tokens) + max(0, event_input.output_tokens)
        if token_count and self.llm_credits_per_1k_tokens:
            token_credits = math.ceil(token_count / 1000) * self.llm_credits_per_1k_tokens
        char_credits = 0
        if event_input.characters and self.tts_credits_per_1k_chars:
            char_credits = math.ceil(event_input.characters / 1000) * self.tts_credits_per_1k_chars
        return token_credits + char_credits

    @staticmethod
    def _reservation_from_row(row) -> CreditReservation:
        return CreditReservation(
            id=row["id"],
            api_key_id=row["api_key_id"],
            amount=row["amount"],
            request_id=row["request_id"],
            state=row["state"],
        )

    @staticmethod
    def _event_from_row(row) -> UsageEvent:
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return UsageEvent(
            id=row["id"],
            api_key_id=row["api_key_id"],
            endpoint=row["endpoint"],
            model=row["model"],
            provider=row["provider"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            characters=row["characters"],
            audio_duration_ms=row["audio_duration_ms"],
            processing_ms=row["processing_ms"],
            gpu_time_ms=row["gpu_time_ms"],
            voice=row["voice"],
            credits_charged=row["credits_charged"],
            status=row["status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=created,
        )
