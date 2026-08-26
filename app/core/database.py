import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 3


class Database:
    """Small thread-safe SQLite wrapper for the gateway's local state."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def initialize(self) -> None:
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    owner_note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    rate_limit_per_minute INTEGER NOT NULL,
                    daily_quota_credits INTEGER NOT NULL,
                    credits INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id TEXT NOT NULL REFERENCES api_keys(id),
                    endpoint TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    characters INTEGER NOT NULL DEFAULT 0,
                    audio_duration_ms INTEGER NOT NULL DEFAULT 0,
                    processing_ms INTEGER NOT NULL DEFAULT 0,
                    gpu_time_ms INTEGER,
                    voice TEXT,
                    credits_charged INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    api_key_id TEXT REFERENCES api_keys(id),
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS credit_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id TEXT NOT NULL REFERENCES api_keys(id),
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    usage_event_id INTEGER REFERENCES usage_events(id),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS credit_reservations (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    api_key_id TEXT NOT NULL REFERENCES api_keys(id),
                    amount INTEGER NOT NULL CHECK (amount >= 0),
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'settled', 'refunded')),
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    usage_event_id INTEGER REFERENCES usage_events(id)
                );

                CREATE TRIGGER IF NOT EXISTS api_keys_credits_nonnegative_insert
                BEFORE INSERT ON api_keys
                WHEN NEW.credits < 0
                BEGIN
                    SELECT RAISE(ABORT, 'credits must be non-negative');
                END;

                CREATE TRIGGER IF NOT EXISTS api_keys_credits_nonnegative_update
                BEFORE UPDATE OF credits ON api_keys
                WHEN NEW.credits < 0
                BEGIN
                    SELECT RAISE(ABORT, 'credits must be non-negative');
                END;

                CREATE INDEX IF NOT EXISTS idx_usage_events_key_created
                    ON usage_events(api_key_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_state_created
                    ON jobs(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_credit_reservations_key_state_created
                    ON credit_reservations(api_key_id, state, created_at);
                """
            )
            current_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {current_version} is newer than supported {SCHEMA_VERSION}")
            if current_version < 3:
                columns = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(api_keys)").fetchall()
                }
                if "label" not in columns:
                    self.connection.execute("ALTER TABLE api_keys ADD COLUMN label TEXT NOT NULL DEFAULT ''")
                if "owner_note" not in columns:
                    self.connection.execute("ALTER TABLE api_keys ADD COLUMN owner_note TEXT NOT NULL DEFAULT ''")
                self.connection.execute("PRAGMA user_version = 3")
                current_version = 3
            if current_version < SCHEMA_VERSION:
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._lock:
            return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, parameters)

    def fetch_one(self, sql: str, parameters: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(sql, parameters).fetchone()

    def fetch_all(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(sql, parameters).fetchall())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
