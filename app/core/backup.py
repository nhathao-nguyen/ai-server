import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def backup_database(database_path: Path, backup_dir: Path, *, keep: int = 5) -> Path:
    """Create and validate an online SQLite backup, then rotate old copies."""

    if keep < 1:
        raise ValueError("keep must be at least one")
    database_path = Path(database_path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"server-{stamp}.db"
    try:
        with sqlite3.connect(database_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    backups = sorted(backup_dir.glob("server-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)
    return destination


def restore_database(backup_path: Path, destination_path: Path) -> Path:
    """Restore a validated SQLite backup atomically to an offline destination."""

    backup_path = Path(backup_path)
    destination_path = Path(destination_path)
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        source = sqlite3.connect(backup_path)
        target = sqlite3.connect(temporary_path)
        try:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("SQLite restore integrity check failed")
        finally:
            target.close()
            source.close()
        temporary_path.replace(destination_path)
        return destination_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
