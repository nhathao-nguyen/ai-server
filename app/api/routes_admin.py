from datetime import datetime, timezone
import asyncio
import json
import queue

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    AppServices,
    get_services,
    require_scope,
)
from app.api.schemas import ApiKeyCreateRequest
from app.auth.service import AVAILABLE_SCOPES
from app.core.errors import ApiError
from app.core.backup import backup_database


router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_scope("admin.full"))],
)


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("invalid_expiration", "expires_at must be an ISO-8601 timestamp", 422) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _public_payload(public) -> dict:
    return {
        "id": public.id,
        "key_prefix": public.key_prefix,
        "label": public.label,
        "owner_note": public.owner_note,
        "scopes": sorted(public.scopes),
        "enabled": public.enabled,
        "expires_at": public.expires_at.isoformat() if public.expires_at else None,
        "rate_limit_per_minute": public.rate_limit_per_minute,
        "daily_quota_credits": public.daily_quota_credits,
        "credits": public.credits,
        "created_at": public.created_at.isoformat(),
        "last_used_at": public.last_used_at.isoformat() if public.last_used_at else None,
        "revoked": public.revoked,
    }


@router.get("/api-keys")
async def list_api_keys(
    include_inactive: bool = Query(default=False),
    services: AppServices = Depends(get_services),
):
    return {
        "keys": [
            _public_payload(item)
            for item in services.auth.list_public(include_inactive=include_inactive)
        ]
    }


@router.get("/scopes")
async def list_scopes():
    return {"scopes": sorted(AVAILABLE_SCOPES)}


@router.get("/overview")
async def admin_overview(services: AppServices = Depends(get_services)):
    # The dashboard polls this endpoint. Re-probing Ollama and local model
    # dependencies on every poll is both expensive and capable of piling up
    # overlapping UI requests. Model refresh remains available through
    # /v1/models and /v1/admin/models; overview reports the latest snapshot.
    statuses = services.registry.current()
    if not statuses:
        statuses = await services.registry.refresh()
    gpu = services.gpu_probe.read() if services.gpu_probe is not None else None
    ram = services.memory_probe.read() if services.memory_probe is not None else None
    return {
        "status": "degraded" if any(not item.available for item in statuses) else "ok",
        "service": services.settings.app_name,
        "models": [item.as_dict() for item in statuses],
        "gpu": gpu.as_dict() if gpu is not None else {"available": False, "reason": "not_configured"},
        "ram": ram.as_dict() if ram is not None else {"total_bytes": None, "used_bytes": None, "utilization_percent": None},
        "scheduler": services.scheduler.get_status(),
    }


@router.get("/build-info")
async def admin_build_info(services: AppServices = Depends(get_services)):
    return services.build_info or {}


@router.post("/api-keys")
async def create_api_key(
    payload: ApiKeyCreateRequest,
    services: AppServices = Depends(get_services),
):
    public, full_key = services.auth.create(
        payload.scopes,
        label=payload.label,
        owner_note=payload.owner_note,
        expires_at=_parse_expiry(payload.expires_at),
        rate_limit_per_minute=payload.rate_limit_per_minute,
        daily_quota_credits=payload.daily_quota_credits,
        initial_credits=payload.initial_credits,
    )
    result = _public_payload(public)
    result["key"] = full_key
    if services.events is not None:
        services.events.publish("api_key.created", message="API key metadata created", metadata={"key_id": public.id})
    return result


@router.post("/api-keys/{key_id}/enable")
async def enable_api_key(
    key_id: str,
    services: AppServices = Depends(get_services),
):
    return _public_payload(services.auth.set_enabled(key_id, True))


@router.post("/api-keys/{key_id}/disable")
async def disable_api_key(
    key_id: str,
    services: AppServices = Depends(get_services),
):
    return _public_payload(services.auth.set_enabled(key_id, False))


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    services: AppServices = Depends(get_services),
):
    result = _public_payload(services.auth.revoke(key_id))
    if services.events is not None:
        services.events.publish("api_key.revoked", message="API key revoked", metadata={"key_id": key_id})
    return result


@router.delete("/api-keys/{key_id}/permanent")
async def permanently_delete_api_key(
    key_id: str,
    services: AppServices = Depends(get_services),
):
    result = services.auth.delete_permanently(key_id)
    if services.events is not None and result.get("status") == "deleted":
        services.events.publish(
            "api_key.deleted",
            message="API key permanently deleted",
            metadata={"key_id": key_id},
        )
    return result


@router.get("/usage")
async def admin_usage(
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    provider: str | None = Query(default=None, max_length=64),
    model: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    services: AppServices = Depends(get_services),
):
    clauses: list[str] = []
    parameters: list[str] = []
    for expression, value in (
        ("created_at >= ?", start),
        ("created_at < ?", end),
        ("provider = ?", provider),
        ("model = ?", model),
        ("status = ?", status),
    ):
        if value:
            clauses.append(expression)
            parameters.append(value)
    where_sql = f"WHERE {' AND '.join(['u.' + c if not c.startswith('u.') else c for c in clauses])}" if clauses else ""
    rows = services.db.fetch_all(
        f"""
        SELECT 
            u.api_key_id,
            COALESCE(k.key_prefix, u.api_key_id) AS key_prefix,
            COALESCE(k.label, '') AS label,
            COALESCE(k.credits, 0) AS credits_remaining,
            COALESCE(k.daily_quota_credits, 0) AS daily_quota_credits,
            COUNT(*) AS events,
            COALESCE(SUM(u.credits_charged), 0) AS credits,
            COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
            COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
            COALESCE(SUM(u.characters), 0) AS characters,
            COALESCE(SUM(u.audio_duration_ms), 0) AS audio_duration_ms
        FROM usage_events u
        LEFT JOIN api_keys k ON u.api_key_id = k.id
        {where_sql}
        GROUP BY u.api_key_id
        ORDER BY events DESC
        """,
        tuple(parameters),
    )
    return {
        "filters": {"start": start, "end": end, "provider": provider, "model": model, "status": status},
        "keys": [dict(row) for row in rows],
    }


@router.get("/gpu")
async def admin_gpu(
    services: AppServices = Depends(get_services),
):
    gpu = services.gpu_probe.read() if services.gpu_probe is not None else None
    return gpu.as_dict() if gpu is not None else {"available": False, "reason": "not_configured"}


@router.get("/metrics")
async def admin_metrics(
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    provider: str | None = Query(default=None, max_length=64),
    model: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    services: AppServices = Depends(get_services),
):
    clauses: list[str] = []
    parameters: list[str] = []
    for expression, value in (
        ("created_at >= ?", start),
        ("created_at < ?", end),
        ("provider = ?", provider),
        ("model = ?", model),
        ("status = ?", status),
    ):
        if value:
            clauses.append(expression)
            parameters.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = services.db.fetch_all(
        f"SELECT status, processing_ms, gpu_time_ms, input_tokens, output_tokens, characters, audio_duration_ms, provider FROM usage_events {where} ORDER BY processing_ms ASC",
        tuple(parameters),
    )
    latencies = [max(0, int(row["processing_ms"] or 0)) for row in rows]
    job_clauses = [
        "started_at IS NOT NULL",
        "finished_at IS NOT NULL",
        "state IN ('succeeded', 'failed', 'interrupted')",
    ]
    job_parameters: list[str] = []
    for expression, value in (
        ("created_at >= ?", start),
        ("created_at < ?", end),
        ("provider = ?", provider),
        ("model = ?", model),
        ("state = ?", status),
    ):
        if value:
            job_clauses.append(expression)
            job_parameters.append(value)
    job_rows = services.db.fetch_all(
        """
        SELECT CAST((julianday(started_at) - julianday(created_at)) * 86400000 AS INTEGER) AS queue_wait_ms,
               CAST((julianday(finished_at) - julianday(started_at)) * 86400000 AS INTEGER) AS generation_ms
        FROM jobs
        WHERE """
        + " AND ".join(job_clauses),
        tuple(job_parameters),
    )
    queue_waits = sorted(max(0, int(row["queue_wait_ms"] or 0)) for row in job_rows)
    generations = sorted(max(0, int(row["generation_ms"] or 0)) for row in job_rows)

    def percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, int((len(values) - 1) * ratio)))
        return values[index]

    count = len(rows)
    succeeded = sum(1 for row in rows if row["status"] == "succeeded")
    failed = sum(1 for row in rows if row["status"] in {"failed", "refunded", "interrupted"})
    lifecycle = {"load": 0, "unload": 0, "restart": 0}
    if services.events is not None:
        for event in services.events.recent(limit=500):
            name = str(event.get("event", ""))
            for action in lifecycle:
                if name.endswith(f".{action}") or name == action:
                    lifecycle[action] += 1
    database_size = 0
    for candidate in (services.db.path, services.db.path.with_name(f"{services.db.path.name}-wal"), services.db.path.with_name(f"{services.db.path.name}-shm")):
        try:
            database_size += candidate.stat().st_size
        except FileNotFoundError:
            pass
    scheduler_status = services.scheduler.get_status()
    return {
        "scheduler": scheduler_status,
        "filters": {"start": start, "end": end, "provider": provider, "model": model, "status": status},
        "request_count": count,
        "success_rate": (succeeded / count) if count else 0.0,
        "error_rate": (failed / count) if count else 0.0,
        "latency_ms": {"p50_total": percentile(latencies, 0.50), "p95_total": percentile(latencies, 0.95), "p50_queue_wait": percentile(queue_waits, 0.50), "p95_queue_wait": percentile(queue_waits, 0.95), "p50_generation": percentile(generations, 0.50), "p95_generation": percentile(generations, 0.95)},
        "totals": {
            "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
            "output_tokens": sum(int(row["output_tokens"] or 0) for row in rows),
            "characters": sum(int(row["characters"] or 0) for row in rows),
            "audio_duration_ms": sum(int(row["audio_duration_ms"] or 0) for row in rows),
            "gpu_time_ms": sum(int(row["gpu_time_ms"] or 0) for row in rows),
            "credits": sum(int(row["credits_charged"] or 0) for row in services.db.fetch_all(f"SELECT credits_charged FROM usage_events {where}", tuple(parameters))),
        },
        "provider_lifecycle": scheduler_status.get("provider_lifecycle", {}),
        "lifecycle_events": lifecycle,
        "memory": {
            "ram": services.memory_probe.read().as_dict() if services.memory_probe is not None else {"total_bytes": None, "available_bytes": None, "used_bytes": None, "utilization_percent": None, "reason": "runtime_probe_not_configured"},
            "gpu": services.gpu_probe.read().as_dict() if services.gpu_probe is not None else None,
        },
        "database_size_bytes": database_size,
        "events_buffered": services.events.latest_sequence if services.events is not None else 0,
    }


@router.post("/backup")
async def create_backup(
    services: AppServices = Depends(get_services),
):
    path = await asyncio.to_thread(backup_database, services.db.path, services.paths.backups)
    if services.events is not None:
        services.events.publish("backup.created", component="storage", message="SQLite backup created")
    return {"status": "ok", "filename": path.name}


@router.get("/jobs")
async def admin_jobs(
    services: AppServices = Depends(get_services),
):
    rows = services.db.fetch_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")
    return {"scheduler": services.scheduler.get_status(), "jobs": [dict(row) for row in rows]}


@router.get("/models")
async def admin_models(
    services: AppServices = Depends(get_services),
):
    statuses = await services.registry.refresh()
    return {"models": [item.as_dict() for item in statuses]}


@router.post("/runtime/reset")
async def reset_runtime(
    services: AppServices = Depends(get_services),
):
    await services.scheduler.reset_runtime()
    if services.events is not None:
        services.events.publish("runtime.reset", message="Runtime reset completed")
    return {"status": "reset", "preserved": ["api_keys", "usage", "credits", "model_cache"]}


@router.get("/events")
async def list_events(
    services: AppServices = Depends(get_services),
):
    events = services.events.recent() if services.events is not None else []
    return {"events": events, "latest_sequence": services.events.latest_sequence if services.events is not None else 0}


@router.get("/events/stream")
async def stream_events(
    after: int | None = Query(default=None, ge=0),
    services: AppServices = Depends(get_services),
):
    if services.events is None:
        return StreamingResponse(iter([": keep-alive\n\n"]), media_type="text/event-stream")

    subscription = services.events.subscribe()

    async def event_body():
        seen: set[str] = set()
        try:
            for event in services.events.recent(after=after):
                seen.add(str(event["id"]))
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(subscription.get, 15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                event_id = str(event["id"])
                if event_id in seen:
                    continue
                seen.add(event_id)
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
        finally:
            subscription.close()

    return StreamingResponse(
        event_body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
