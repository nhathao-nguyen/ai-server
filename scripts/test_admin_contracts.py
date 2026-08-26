"""Run admin contract checks against a real isolated Uvicorn process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def request(base: str, path: str, *, method: str = "GET", key: str = "", body: Any = None) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8156)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tts-server-admin-contract-") as temp_dir:
        data_dir = Path(temp_dir)
        env = os.environ.copy()
        env.update({"DATA_DIR": str(data_dir), "HOST": "127.0.0.1", "PORT": str(args.port)})
        sys.path.insert(0, str(ROOT))
        from app.auth.service import ApiKeyService
        from app.core.database import Database

        db = Database(data_dir / "server.db")
        db.initialize()
        admin_key = ApiKeyService(db).create({"admin.full"})[1]
        db.close()
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        base = f"http://127.0.0.1:{args.port}"
        try:
            for _ in range(80):
                status, payload = request(base, "/health/live")
                if status == 200 and payload.get("status") == "ok":
                    break
                time.sleep(0.25)
            else:
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"isolated server did not become ready: {stderr[-2000:]}")

            status, overview = request(base, "/v1/admin/overview", key=admin_key)
            assert status == 200 and int(overview["runtime"]["pid"]) > 0 and process.poll() is None, overview
            status, settings = request(base, "/v1/admin/settings/effective", key=admin_key)
            assert status == 200 and settings["host"] == "127.0.0.1" and settings["port"] == args.port
            status, jobs = request(base, "/v1/admin/jobs?page=1&page_size=25&search=no-such-job", key=admin_key)
            assert status == 200 and jobs["page"] == 1 and jobs["page_size"] == 25 and jobs["total"] == 0
            status, usage = request(base, "/v1/admin/usage/events?page=1&page_size=25", key=admin_key)
            assert status == 200 and usage["page"] == 1 and usage["page_size"] == 25
            status, events = request(base, "/v1/admin/events?page=1&page_size=25", key=admin_key)
            assert status == 200 and events["page"] == 1 and events["page_size"] == 25

            status, created = request(
                base,
                "/v1/admin/api-keys",
                method="POST",
                key=admin_key,
                body={"scopes": ["tts.generate"], "label": "http-regression"},
            )
            assert status == 200
            key_id = str(created["id"])
            assert request(base, f"/v1/admin/api-keys/{key_id}", method="DELETE", key=admin_key)[0] == 200
            revoke_again = request(base, f"/v1/admin/api-keys/{key_id}", method="DELETE", key=admin_key)
            assert revoke_again[0] == 200 and revoke_again[1]["enabled"] is False
            delete_first = request(base, f"/v1/admin/api-keys/{key_id}/permanent", method="DELETE", key=admin_key)
            delete_again = request(base, f"/v1/admin/api-keys/{key_id}/permanent", method="DELETE", key=admin_key)
            assert delete_first[0] == 200 and delete_first[1]["status"] == "deleted"
            assert delete_again[0] == 200 and delete_again[1]["status"] == "already_deleted"
            print(json.dumps({
                "status": "PASS",
                "pid": overview["runtime"]["pid"],
                "bind": f"{overview['runtime']['bind_address']}:{overview['runtime']['port']}",
                "settings_source": settings["source"],
                "delete_first": delete_first[1]["status"],
                "delete_second": delete_again[1]["status"],
            }, ensure_ascii=False))
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)


if __name__ == "__main__":
    raise SystemExit(main())
