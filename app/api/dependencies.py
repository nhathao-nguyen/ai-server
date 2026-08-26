import ipaddress
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from app.auth.models import ApiKeyPrincipal
from app.auth.rate_limit import RateLimiter
from app.core.errors import ApiError


@dataclass
class AppServices:
    settings: Any
    paths: Any
    db: Any
    auth: Any
    usage: Any
    rate_limiter: RateLimiter | None
    scheduler: Any
    registry: Any
    engine_router: Any
    ollama: Any
    chatterbox: Any
    vieneu: Any
    gpu_probe: Any
    events: Any | None = None
    audio_probe: Any | None = None
    build_info: dict[str, Any] | None = None
    memory_probe: Any | None = None


def get_services(request: Request) -> AppServices:
    return request.app.state.services


async def current_principal(
    request: Request,
    services: AppServices = Depends(get_services),
) -> ApiKeyPrincipal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError("authentication_required", "A bearer API key is required", 401)
    principal = services.auth.authenticate(token.strip())
    if services.rate_limiter is not None:
        services.rate_limiter.check(principal.id, principal.rate_limit_per_minute)
    return principal


def require_scope(scope: str):
    async def dependency(principal: ApiKeyPrincipal = Depends(current_principal)) -> ApiKeyPrincipal:
        if not principal.has_scope(scope):
            raise ApiError("scope_required", f"Scope '{scope}' is required", 403, {"scope": scope})
        return principal

    return dependency


def require_loopback(request: Request) -> None:
    host = request.client.host if request.client is not None else None
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ApiError("loopback_only", "This desktop control is available on loopback only", 403)


def is_lan_client(host: str | None) -> bool:
    """Return whether a peer is loopback or in an RFC1918 IPv4 LAN."""

    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if address.is_loopback:
        return True
    return address.version == 4 and (
        address in ipaddress.ip_network("10.0.0.0/8")
        or address in ipaddress.ip_network("172.16.0.0/12")
        or address in ipaddress.ip_network("192.168.0.0/16")
    )
