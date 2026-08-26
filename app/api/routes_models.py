from fastapi import APIRouter, Depends

from app.api.dependencies import AppServices, current_principal, get_services


router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models")
async def list_models(
    _principal=Depends(current_principal),
    services: AppServices = Depends(get_services),
):
    statuses = await services.registry.refresh()
    models = [item.as_dict() for item in statuses]
    return {"object": "list", "data": models, "models": models}
