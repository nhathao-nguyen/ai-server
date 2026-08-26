import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse

logger = logging.getLogger("tts_server")

from app.api.dependencies import AppServices, is_lan_client, require_loopback
from app.api.routes_audio import router as audio_router
from app.api.routes_admin import router as admin_router
from app.api.routes_chat import router as chat_router
from app.api.routes_models import router as models_router
from app.api.routes_usage import router as usage_router
from app.auth.rate_limit import RateLimiter
from app.auth.service import ApiKeyService
from app.core.config import Settings
from app.core.backup import backup_database
from app.core.database import SCHEMA_VERSION, Database
from app.core.errors import ApiError
from app.core.events import EventBuffer
from app.core.gpu import GpuStatusProbe
from app.core.logging import configure_logging
from app.core.model_registry import ModelRegistry
from app.core.media import probe_audio_duration_seconds
from app.core.memory import SystemMemoryProbe
from app.core.paths import AppPaths
from app.core.reconciliation import reconcile_runtime_state
from app.core.retention import prune_retention
from app.core.source_identity import build_info
from app.engines.chatterbox import ChatterboxProvider
from app.engines.ollama import OllamaProvider
from app.engines.router import EngineRouter
from app.engines.vieneu import VieneuProvider
from app.scheduler.gpu_lock import GpuLock
from app.scheduler.manager import JobManager
from app.usage.service import UsageService


DESKTOP_CORS_ORIGINS = (
    "http://tauri.localhost",
    "tauri://localhost",
    "http://localhost:1420",
)


def build_services(settings: Settings) -> AppServices:
    paths = AppPaths.from_settings(settings)
    db = Database(paths.database)
    auth = ApiKeyService(
        db,
        default_rate_limit_per_minute=settings.default_rate_limit_per_minute,
        default_daily_quota_credits=settings.default_daily_quota_credits,
        default_initial_credits=settings.default_initial_credits,
    )
    usage = UsageService(
        db,
        llm_credits_per_1k_tokens=settings.llm_credits_per_1k_tokens,
        tts_credits_per_1k_chars=settings.tts_credits_per_1k_chars,
    )
    ollama = OllamaProvider(
        settings.ollama_url,
        settings.ollama_model,
        settings.gpu_job_timeout_seconds,
        keep_alive=settings.ollama_keep_alive,
    )
    chatterbox = ChatterboxProvider(
        paths.huggingface_cache,
        manifest_path=paths.manifests / "chatterbox.json",
        timeout_seconds=settings.gpu_job_timeout_seconds,
        temp_dir=paths.temp,
    )
    vieneu = VieneuProvider(
        paths.huggingface_cache,
        manifest_path=paths.manifests / "vieneu.json",
        timeout_seconds=settings.gpu_job_timeout_seconds,
        temp_dir=paths.temp,
        runtime_config={
            "mode": settings.vieneu_mode,
            "device": settings.vieneu_device,
            "backend": settings.vieneu_backend,
            "precision": settings.vieneu_precision,
            "threads": settings.vieneu_threads,
            "max_batch_size": settings.vieneu_max_batch_size,
        },
    )
    gpu_lock = GpuLock(settings.max_gpu_ai_jobs, switch_unload=settings.gpu_switch_unload)
    gpu_lock.register_provider("ollama", ollama)
    gpu_lock.register_provider("chatterbox", chatterbox)
    registry = ModelRegistry(
        settings,
        paths,
        ollama_probe=ollama,
        providers={"ollama": ollama, "chatterbox": chatterbox, "vieneu": vieneu},
        capability_probe=gpu_lock.capability_probe,
    )
    events = EventBuffer()
    source_root = Path(__file__).resolve().parents[1]
    return AppServices(
        settings=settings,
        paths=paths,
        db=db,
        auth=auth,
        usage=usage,
        rate_limiter=RateLimiter(),
        scheduler=JobManager(
            db,
            gpu_lock,
            cpu_provider=vieneu,
            idle_sleep_seconds=settings.model_idle_sleep_seconds,
            idle_reaper_interval_seconds=settings.idle_reaper_interval_seconds,
            gpu_affinity_batch_size=settings.gpu_affinity_batch_size,
            max_gpu_queue=settings.max_gpu_queue,
            max_cpu_queue=settings.max_cpu_queue,
            max_concurrent_jobs_per_key=settings.max_concurrent_jobs_per_key,
        ),
        registry=registry,
        engine_router=EngineRouter(settings.ollama_model),
        ollama=ollama,
        chatterbox=chatterbox,
        vieneu=vieneu,
        gpu_probe=GpuStatusProbe(),
        memory_probe=SystemMemoryProbe(),
        events=events,
        audio_probe=probe_audio_duration_seconds,
        build_info=build_info(source_root, manifest_dir=paths.manifests),
    )


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    settings = settings or Settings()
    services = services or build_services(settings)
    if services.rate_limiter is None:
        services.rate_limiter = RateLimiter()
    if services.events is None:
        services.events = EventBuffer()
    logger = configure_logging(settings.log_level, services.paths.logs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.paths.ensure_directories()
        previous_schema = services.db.schema_version()
        if 0 < previous_schema < SCHEMA_VERSION and services.db.path.is_file():
            backup = await asyncio.to_thread(
                backup_database,
                services.db.path,
                services.paths.backups,
            )
            logger.info("database migration backup created; schema=%s file=%s", previous_schema, backup.name)
        services.db.initialize()
        retention = prune_retention(
            services.db,
            services.paths.logs,
            job_retention_days=settings.job_retention_days,
            usage_retention_days=settings.usage_retention_days,
            log_retention_days=settings.log_retention_days,
        )
        if any(retention.values()):
            logger.info(
                "retention applied; jobs_deleted=%s usage_events_deleted=%s log_files_deleted=%s",
                retention["jobs_deleted"],
                retention["usage_events_deleted"],
                retention["log_files_deleted"],
            )
        reconciliation = reconcile_runtime_state(services.db)
        if any(reconciliation.values()):
            logger.info(
                "runtime state reconciled; interrupted_jobs=%s refunded_reservations=%s",
                reconciliation["interrupted_jobs"],
                reconciliation["refunded_reservations"],
            )
        legacy_keys = services.auth.sanitize_legacy_keys()
        if any(legacy_keys.values()):
            logger.info(
                "legacy API keys sanitized; updated=%s removed=%s tombstoned=%s",
                legacy_keys["updated"],
                legacy_keys["removed"],
                legacy_keys["tombstoned"],
            )
        await services.scheduler.start()
        try:
            await services.registry.refresh()
        except Exception as exc:
            logger.warning("model registry refresh failed: %s", type(exc).__name__)
        logger.info("server started; model registry initialized")
        services.events.publish("server.started", message="FastAPI server started")
        try:
            yield
        finally:
            await services.scheduler.stop()
            if hasattr(services.vieneu, "stop"):
                await services.vieneu.stop()
            services.db.close()
            services.events.publish("server.stopped", message="FastAPI server stopped")
            logger.info("server stopped")

    app = FastAPI(
        title=settings.app_name,
        version=str((services.build_info or {}).get("version") or "0.1.0"),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services

    if settings.lan_only:
        @app.middleware("http")
        async def lan_only_guard(request: Request, call_next):
            host = request.client.host if request.client is not None else None
            if not is_lan_client(host):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "lan_only",
                            "message": "This server accepts connections only from loopback or RFC1918 LAN peers",
                        }
                    },
                )
            return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DESKTOP_CORS_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=[
            "X-TTS-Model",
            "X-TTS-Provider",
            "X-TTS-Characters",
            "X-TTS-Duration-Ms",
            "X-TTS-Generation-Ms",
            "X-TTS-Credits",
            "X-TTS-Voice",
            "X-TTS-Effective-Options",
            "X-TTS-Speed",
        ],
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError):
        headers = {}
        if "retry_after" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after"])
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Dữ liệu yêu cầu không hợp lệ",
                    "details": exc.errors(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(_request: Request, exc: Exception):
        logger.exception("unhandled server exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Đã xảy ra lỗi máy chủ nội bộ",
                    "details": {"error_type": type(exc).__name__},
                }
            },
        )

    @app.get("/health/live", tags=["system"])
    async def live_health():
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": str((services.build_info or {}).get("version") or "0.1.0"),
        }

    async def detailed_health(request: Request):
        require_loopback(request)
        models = await services.registry.refresh()
        gpu = services.gpu_probe.read() if services.gpu_probe is not None else None
        model_payload = [item.as_dict() for item in models]
        degraded = any(not item.available for item in models)
        return {
            "status": "degraded" if degraded else "ok",
            "service": settings.app_name,
            "models": model_payload,
            "gpu": gpu.as_dict() if gpu is not None else {"available": False, "reason": "not_configured"},
            "scheduler": services.scheduler.get_status(),
        }

    @app.get("/health", tags=["system"])
    async def health(request: Request):
        return await detailed_health(request)

    @app.get("/health/ready", tags=["system"])
    async def ready_health(request: Request):
        return await detailed_health(request)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json(request: Request):
        require_loopback(request)
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request):
        require_loopback(request)
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{settings.app_name} docs")

    @app.get("/redoc", include_in_schema=False)
    async def redoc(request: Request):
        require_loopback(request)
        return get_redoc_html(openapi_url="/openapi.json", title=f"{settings.app_name} docs")

    app.include_router(chat_router)
    app.include_router(audio_router)
    app.include_router(admin_router)
    app.include_router(models_router)
    app.include_router(usage_router)
    return app


app = create_app()
