"""Create the first full-control API key for a headless server."""

import argparse
import json
from pathlib import Path

from app.auth.service import ApiKeyService
from app.core.config import Settings
from app.core.database import Database
from app.core.paths import AppPaths


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a full-control server admin API key")
    parser.add_argument("--label", default="server-admin", help="Human-readable key label")
    parser.add_argument("--owner-note", default="", help="Optional owner note")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override the server data directory")
    args = parser.parse_args()

    settings = Settings(data_dir=args.data_dir) if args.data_dir is not None else Settings()
    paths = AppPaths.from_settings(settings)
    paths.ensure_directories()
    db = Database(paths.database)
    db.initialize()
    try:
        auth = ApiKeyService(
            db,
            default_rate_limit_per_minute=settings.default_rate_limit_per_minute,
            default_daily_quota_credits=settings.default_daily_quota_credits,
            default_initial_credits=settings.default_initial_credits,
        )
        public, full_key = auth.create(
            {"admin.full"},
            label=args.label,
            owner_note=args.owner_note,
        )
        print(json.dumps({"id": public.id, "key_prefix": public.key_prefix, "key": full_key}, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
