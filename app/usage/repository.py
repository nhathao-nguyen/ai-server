from datetime import datetime, timezone

from app.core.database import Database


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


class UsageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def daily_credits(self, api_key_id: str, now: datetime | None = None) -> int:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = self.db.fetch_one(
            "SELECT COALESCE(SUM(credits_charged), 0) AS total FROM usage_events WHERE api_key_id = ? AND status = 'succeeded' AND created_at >= ?",
            (api_key_id, start),
        )
        return int(row["total"] if row else 0)

    def recent(self, api_key_id: str, limit: int = 50) -> list[dict]:
        rows = self.db.fetch_all(
            "SELECT * FROM usage_events WHERE api_key_id = ? ORDER BY id DESC LIMIT ?",
            (api_key_id, max(1, min(limit, 200))),
        )
        return [dict(row) for row in rows]
