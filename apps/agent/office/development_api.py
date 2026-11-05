"""The demo's explicit local operator surface; no remote release or code-execution endpoint."""
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from office.development import DevelopmentError


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    text: str = Field(min_length=1, max_length=8000)


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    auto_merge: bool = Field(default=False, strict=True)


def local_operator(request: Request):
    # A remote website cannot dispatch a paid coding job through a localhost browser.
    # This is a loopback demo boundary, not multi-user production authentication.
    peer = request.client.host if request.client else ""
    try:
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
    except ValueError:
        raise HTTPException(403, "Invalid local host") from None
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "cf-connecting-ip")):
        raise HTTPException(403, "Development controls cannot be accessed through a gateway")
    if peer not in {"127.0.0.1", "::1", "testclient"} or host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        raise HTTPException(403, "Development controls are available only on this computer")
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port in {3001, 5173}
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(403, "Use the local dashboard to manage development requests")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site development requests are refused")


def router(service):
    routes = APIRouter()

    @routes.get("/api/development")
    async def snapshot(request: Request):
        local_operator(request)
        return service.snapshot()

    @routes.post("/api/development")
    async def draft(body: Draft, request: Request):
        local_operator(request)
        try:
            return service.draft(body.id, body.text)
        except DevelopmentError as error:
            raise HTTPException(error.status, str(error)) from None

    @routes.post("/api/development/{request_id}/send")
    async def send(request_id: str, body: Send, request: Request):
        local_operator(request)
        try:
            return await service.send(request_id, body.fingerprint, body.auto_merge)
        except DevelopmentError as error:
            raise HTTPException(error.status, str(error)) from None

    @routes.post("/api/development/{request_id}/refresh")
    async def refresh(request_id: str, request: Request):
        local_operator(request)
        try:
            return await service.refresh(request_id)
        except DevelopmentError as error:
            raise HTTPException(error.status, str(error)) from None

    return routes
