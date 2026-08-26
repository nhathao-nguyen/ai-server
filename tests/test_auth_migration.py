from datetime import datetime, timezone
from pathlib import Path

from app.auth.service import ApiKeyService
from app.core.database import Database


def test_sanitize_legacy_key_with_reservation_tombstones(tmp_path: Path) -> None:
    db = Database(tmp_path / "server.db")
    db.initialize()
    service = ApiKeyService(db)
    public, _full_key = service.create({"admin.full"})
    db.execute("UPDATE api_keys SET scopes_json = '[\"legacy\"]' WHERE id = ?", (public.id,))
    db.execute(
        "INSERT INTO credit_reservations(id, request_id, api_key_id, amount, state, created_at) VALUES (?, ?, ?, ?, 'reserved', ?)",
        ("reservation", "request", public.id, 1, datetime.now(timezone.utc).isoformat()),
    )
    result = service.sanitize_legacy_keys()
    row = db.fetch_one("SELECT enabled, revoked_at FROM api_keys WHERE id = ?", (public.id,))
    assert result["tombstoned"] == 1
    assert row["enabled"] == 0
    assert row["revoked_at"] is not None
    db.close()
