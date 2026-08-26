from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.database import Database


TERMINAL_JOB_STATES = ("succeeded", "failed", "interrupted")


def _cutoff(now: datetime, days: int) -> str:
    current = now.astimezone(timezone.utc)
    return (current - timedelta(days=days)).isoformat()


def _delete_expired_jobs(db: Database, cutoff: str, batch_size: int) -> int:
    deleted = 0
    while True:
        with db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE state IN (?, ?, ?) AND finished_at IS NOT NULL AND finished_at < ?
                ORDER BY finished_at LIMIT ?
                """,
                (*TERMINAL_JOB_STATES, cutoff, batch_size),
            ).fetchall()
            if not rows:
                return deleted
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            cursor = connection.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
            deleted += cursor.rowcount


def _delete_expired_usage(db: Database, cutoff: str, batch_size: int) -> int:
    deleted = 0
    while True:
        with db.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM usage_events WHERE created_at < ? ORDER BY id LIMIT ?",
                (cutoff, batch_size),
            ).fetchall()
            if not rows:
                return deleted
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE credit_ledger SET usage_event_id = NULL WHERE usage_event_id IN ({placeholders})",
                ids,
            )
            connection.execute(
                f"UPDATE credit_reservations SET usage_event_id = NULL WHERE usage_event_id IN ({placeholders})",
                ids,
            )
            cursor = connection.execute(f"DELETE FROM usage_events WHERE id IN ({placeholders})", ids)
            deleted += cursor.rowcount


def _delete_expired_logs(logs_path: Path, cutoff_timestamp: float, batch_size: int) -> int:
    if not logs_path.exists():
        return 0
    candidates = [path for path in logs_path.rglob("*") if path.is_file() and path.stat().st_mtime < cutoff_timestamp]
    deleted = 0
    for start in range(0, len(candidates), batch_size):
        for path in candidates[start : start + batch_size]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            deleted += 1
    return deleted


def prune_retention(
    db: Database,
    logs_path: Path,
    *,
    job_retention_days: int,
    usage_retention_days: int,
    log_retention_days: int,
    now: datetime | None = None,
    batch_size: int = 1000,
) -> dict[str, int]:
    """Prune bounded, non-active data while preserving accounting references."""

    if min(job_retention_days, usage_retention_days, log_retention_days) < 1:
        raise ValueError("retention periods must be at least one day")
    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    current = now or datetime.now(timezone.utc)
    job_cutoff = _cutoff(current, job_retention_days)
    usage_cutoff = _cutoff(current, usage_retention_days)
    log_cutoff = (current.astimezone(timezone.utc) - timedelta(days=log_retention_days)).timestamp()
    return {
        "jobs_deleted": _delete_expired_jobs(db, job_cutoff, batch_size),
        "usage_events_deleted": _delete_expired_usage(db, usage_cutoff, batch_size),
        "log_files_deleted": _delete_expired_logs(logs_path, log_cutoff, batch_size),
    }
