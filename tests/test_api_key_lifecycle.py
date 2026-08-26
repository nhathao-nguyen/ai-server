from datetime import datetime, timezone
from pathlib import Path

from app.auth.service import ApiKeyService
from app.core.database import Database


def test_revoke_is_idempotent_and_preserves_original_timestamp(tmp_path: Path) -> None:
    db = Database(tmp_path / "server.db")
    db.initialize()
    service = ApiKeyService(db)
    public, _full_key = service.create({"tts.generate"})

    first = service.revoke(public.id)
    first_timestamp = db.fetch_one("SELECT revoked_at FROM api_keys WHERE id = ?", (public.id,))["revoked_at"]
    second = service.revoke(public.id)
    second_timestamp = db.fetch_one("SELECT revoked_at FROM api_keys WHERE id = ?", (public.id,))["revoked_at"]

    assert first.enabled is False
    assert second.enabled is False
    assert second.revoked is True
    assert db.fetch_one("SELECT enabled FROM api_keys WHERE id = ?", (public.id,))["enabled"] == 0
    assert first_timestamp
    assert second_timestamp == first_timestamp
    db.close()


def test_permanent_delete_removes_key_and_all_dependent_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "server.db")
    db.initialize()
    service = ApiKeyService(db)
    public, _full_key = service.create({"tts.generate"})
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO jobs (id, api_key_id, kind, provider, model, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("job", public.id, "tts", "test", "model", "succeeded", now),
    )
    cursor = db.execute(
        """
        INSERT INTO usage_events
            (api_key_id, endpoint, model, provider, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (public.id, "/test", "model", "test", "succeeded", now),
    )
    usage_id = cursor.lastrowid
    db.execute(
        "INSERT INTO credit_ledger (api_key_id, amount, reason, usage_event_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (public.id, -1, "test", usage_id, now),
    )
    db.execute(
        """
        INSERT INTO credit_reservations
            (id, request_id, api_key_id, amount, state, created_at, usage_event_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("reservation", "request", public.id, 1, "settled", now, usage_id),
    )

    result = service.delete_permanently(public.id)

    assert result["status"] == "deleted"
    assert result["api_keys"] == 1
    assert all(
        db.fetch_one(f"SELECT COUNT(*) AS count FROM {table} WHERE api_key_id = ?", (public.id,))["count"] == 0
        for table in ("jobs", "usage_events", "credit_ledger", "credit_reservations")
    )
    assert db.fetch_one("SELECT id FROM api_keys WHERE id = ?", (public.id,)) is None
    assert service.delete_permanently(public.id)["status"] == "already_deleted"
    db.close()
