from fastapi import APIRouter, Depends, Query

from app.api.dependencies import AppServices, get_services, require_scope
from app.auth.models import ApiKeyPrincipal


router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage")
async def get_usage(
    principal: ApiKeyPrincipal = Depends(require_scope("usage.read")),
    services: AppServices = Depends(get_services),
    limit: int = Query(default=50, ge=1, le=200),
):
    events = services.usage.repository.recent(principal.id, limit)
    return {"total_events": len(events), "events": events}
