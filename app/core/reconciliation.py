from datetime import datetime, timezone

from app.core.database import Database
from app.usage.repository import utc_iso


def reconcile_runtime_state(db: Database, now: datetime | None = None) -> dict[str, int]:
    finished_at = utc_iso(now or datetime.now(timezone.utc))
    interrupted_jobs = 0
    refunded_reservations = 0
    with db.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET state = 'interrupted', finished_at = COALESCE(finished_at, ?),
                error_code = 'runtime_restarted',
                error_message = 'Runtime restarted before job completion'
            WHERE state IN ('queued', 'running')
            """,
            (finished_at,),
        )
        interrupted_jobs = cursor.rowcount
        reservations = connection.execute(
            "SELECT id, api_key_id, amount FROM credit_reservations WHERE state = 'reserved'"
        ).fetchall()
        for reservation in reservations:
            connection.execute(
                "UPDATE api_keys SET credits = credits + ? WHERE id = ?",
                (reservation["amount"], reservation["api_key_id"]),
            )
            connection.execute(
                """
                INSERT INTO credit_ledger (api_key_id, amount, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    reservation["api_key_id"],
                    reservation["amount"],
                    f"reconcile_refund:{reservation['id']}",
                    finished_at,
                ),
            )
            connection.execute(
                "UPDATE credit_reservations SET state = 'refunded', settled_at = ? WHERE id = ?",
                (finished_at, reservation["id"]),
            )
            refunded_reservations += 1
    return {"interrupted_jobs": interrupted_jobs, "refunded_reservations": refunded_reservations}
